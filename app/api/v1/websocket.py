"""ASGI WebSocket Gateway endpoint with JWT authentication and scoped Redis channels.

Handles real-time connections for both Staff and Guests. Enforces presence
verification for guests and role/branch-based scoping for staff.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.security import decode_access_token
from app.core.session_security import (
    GuestSessionJWTError,
    decode_guest_session_jwt,
)
from app.core.websocket_manager import ws_manager
from app.models.auth import Branch, User
from app.models.enums import UserRole

logger = logging.getLogger("app.api.v1.websocket")

router = APIRouter(tags=["realtime"])

# Standard WebSocket Application Close Codes
WS_4001_UNAUTHORIZED = 4001
WS_4003_FORBIDDEN = 4003


def get_session_factory():
    """Dependency returning the database async_session_factory."""
    return async_session_factory


@router.websocket("/ws")
async def websocket_gateway(
    websocket: WebSocket,
    token: str | None = Query(None),
    branch_id: str | None = Query(None),
    channel: str | None = Query(None),
    session_maker: Any = Depends(get_session_factory),
) -> None:
    """Real-time ASGI WebSocket gateway endpoint.

    Authenticates connections via query-parameter JWT (Staff access token or
    verified Guest session token). Enforces role/branch access and routes events
    to scoped in-memory and Redis Pub/Sub channels.
    """
    if not token or not token.strip():
        # Reject immediately prior to accepting
        await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Missing authentication token")
        return

    token_str = token.strip()
    channels: set[str] = set()
    client_identity: dict[str, Any] = {}

    # Inspect unverified token payload to distinguish Guest session from Staff JWT
    try:
        unverified_payload = jwt.decode(
            token_str,
            options={"verify_signature": False},
        )
    except Exception:
        await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Malformed token")
        return

    token_type = unverified_payload.get("type")

    # -------------------------------------------------------------------------
    # Flow A: Guest Session Handshake
    # -------------------------------------------------------------------------
    if token_type == "guest_session":
        try:
            guest_payload = decode_guest_session_jwt(token_str)
        except GuestSessionJWTError as exc:
            await websocket.close(code=WS_4001_UNAUTHORIZED, reason=str(exc))
            return
        except Exception:
            await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Invalid guest session token")
            return

        # Enforce presence verification handshake check
        if not guest_payload.get("is_presence_verified"):
            logger.warning(
                "Guest WebSocket rejected: presence verification required (table %s, branch %s)",
                guest_payload.get("table_id"),
                guest_payload.get("branch_id"),
            )
            await websocket.close(
                code=WS_4003_FORBIDDEN,
                reason="Presence verification required",
            )
            return

        g_branch_id = guest_payload["branch_id"]
        g_table_id = guest_payload["table_id"]
        g_session_id = guest_payload["sub"]

        # Guest is strictly scoped to table-level, session-level, and branch catalog channels
        channels.add(f"branch_{g_branch_id}_table_{g_table_id}")
        channels.add(f"branch_{g_branch_id}_session_{g_session_id}")
        channels.add(f"branch_{g_branch_id}_catalog")

        client_identity = {
            "type": "guest",
            "session_id": g_session_id,
            "branch_id": g_branch_id,
            "table_id": g_table_id,
            "table_number": guest_payload.get("table_number"),
        }

    # -------------------------------------------------------------------------
    # Flow B: Staff User Handshake
    # -------------------------------------------------------------------------
    else:
        try:
            staff_payload = decode_access_token(token_str)
            user_id_raw = staff_payload.get("sub")
            tenant_id_raw = staff_payload.get("tenant_id")
            if not user_id_raw or not tenant_id_raw:
                await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Incomplete staff token claims")
                return

            user_id = uuid.UUID(user_id_raw)
            tenant_id = uuid.UUID(tenant_id_raw)
        except (jwt.PyJWTError, ValueError):
            await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Invalid or expired staff token")
            return

        # Query database to confirm active user and branch access
        try:
            async with session_maker() as db:
                stmt = (
                    select(User)
                    .options(selectinload(User.branch_access))
                    .where(
                        User.id == user_id,
                        User.tenant_id == tenant_id,
                        User.is_active.is_(True),
                    )
                )
                res = await db.execute(stmt)
                user = res.scalar_one_or_none()
        except Exception as exc:
            logger.error("DB error validating staff WebSocket user: %s", exc)
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Authentication lookup failed")
            return

        if not user:
            await websocket.close(code=WS_4001_UNAUTHORIZED, reason="Staff user not found or inactive")
            return

        allowed_branch_ids = {access.branch_id for access in user.branch_access}
        is_super_admin = user.role == UserRole.SUPER_ADMIN
        admin_roles = {UserRole.SUPER_ADMIN, UserRole.REGIONAL_MANAGER, UserRole.BRANCH_ADMIN}

        target_branches: list[uuid.UUID] = []

        # If branch_id was explicitly requested via query parameter
        if branch_id:
            try:
                requested_branch_id = uuid.UUID(branch_id.strip())
            except ValueError:
                await websocket.close(code=WS_4003_FORBIDDEN, reason="Malformed branch_id parameter")
                return

            if not is_super_admin and requested_branch_id not in allowed_branch_ids:
                await websocket.close(code=WS_4003_FORBIDDEN, reason="Branch access unauthorized")
                return
            target_branches = [requested_branch_id]
        else:
            # If branch_id not provided, assign all authorized branches
            if is_super_admin:
                if allowed_branch_ids:
                    target_branches = list(allowed_branch_ids)
                else:
                    # Query all active branches within tenant for Super Admin
                    async with session_maker() as db:
                        b_stmt = select(Branch.id).where(
                            Branch.tenant_id == user.tenant_id,
                            Branch.is_active.is_(True),
                        )
                        b_res = await db.execute(b_stmt)
                        target_branches = list(b_res.scalars().all())
            else:
                target_branches = list(allowed_branch_ids)

        if not target_branches:
            await websocket.close(code=WS_4003_FORBIDDEN, reason="No branch access assigned")
            return

        # Build role-scoped channels across target branches
        for b_id in target_branches:
            # All staff receive general branch staff notifications
            channels.add(f"branch_{b_id}_staff")
            # All staff receive branch catalog availability notifications
            channels.add(f"branch_{b_id}_catalog")

            # Standardized runners channel: subscribed by WAITER, RUNNER, and ADMINs
            if user.role == UserRole.WAITER or str(user.role).upper() == "RUNNER" or user.role in admin_roles:
                channels.add(f"branch_{b_id}_runners")

            # Kitchen channel: subscribed by KITCHEN_STAFF and ADMINs
            if user.role == UserRole.KITCHEN_STAFF or user.role in admin_roles:
                channels.add(f"branch_{b_id}_kitchen")

            # Cashier channel: subscribed by CASHIER and ADMINs
            if user.role == UserRole.CASHIER or user.role in admin_roles:
                channels.add(f"branch_{b_id}_cashier")

            # Floor channel: subscribed when explicitly requested (e.g. ?channel=floor)
            if channel == "floor" or (channel and "floor" in channel.split(",")):
                if user.role in (UserRole.WAITER, UserRole.CASHIER) or user.role in admin_roles:
                    channels.add(f"branch_{b_id}_floor")

            # Admin management channel
            if user.role in admin_roles:
                channels.add(f"branch_{b_id}_admin")

        client_identity = {
            "type": "staff",
            "user_id": str(user.id),
            "role": user.role.value,
            "branches": [str(b) for b in target_branches],
        }

    # -------------------------------------------------------------------------
    # Register Connection and Begin Event Loop
    # -------------------------------------------------------------------------
    await ws_manager.connect(websocket, channels)

    try:
        # Deliver connection confirmation frame
        await websocket.send_json({
            "event": "CONNECTED",
            "channels": sorted(list(channels)),
            "identity": client_identity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        while True:
            # Client message handler (supports keep-alive ping/pong)
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
                continue

            try:
                data = json.loads(message)
                action = data.get("action") or data.get("type") or data.get("event")
                if action == "ping":
                    await websocket.send_json({
                        "event": "PONG",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception:
                # Discard unrecognized non-json messages
                pass

    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected normally")
    except Exception as exc:
        logger.warning("WebSocket client connection error: %s", exc)
    finally:
        await ws_manager.disconnect(websocket)
