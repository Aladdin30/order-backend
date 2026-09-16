"""Background SLA Monitoring Engine and Automated Overdue Escalation Service (Task BE-3.4).

Monitors table quick-service requests (WATER, CUTLERY, WAITER_CALL, OTHER).
When a request remains in PENDING status for more than 3 minutes (180 seconds),
it is atomically escalated (is_escalated=True, escalated_at=now) and broadcast
in real time across both Runners (branch_{branch_id}_runners) and Admin
(branch_{branch_id}_admin) WebSocket channels.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis_pubsub import redis_pubsub
from app.models.enums import ServiceRequestStatus
from app.models.service import ServiceRequest
from app.services.audit_service import AuditLogger

logger = logging.getLogger("app.services.sla_monitor_service")

# Default SLA threshold: 3 minutes (180 seconds)
DEFAULT_SLA_THRESHOLD_SECONDS: int = 180
DEFAULT_MONITOR_INTERVAL_SECONDS: int = 15


def _ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    """Ensure a datetime object is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


async def check_and_escalate_overdue_requests(
    db: AsyncSession,
    threshold_seconds: int = DEFAULT_SLA_THRESHOLD_SECONDS,
) -> int:
    """Query, atomically lock, and escalate overdue table service requests.

    Uses `with_for_update(skip_locked=True)` to prevent race conditions across
    multi-process ASGI workers. Broadcasts real-time escalation frames strictly
    after `await db.commit()`.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff_time = now - datetime.timedelta(seconds=threshold_seconds)

    # 1. Fetch overdue pending requests with row-level lock
    stmt = (
        select(ServiceRequest)
        .options(
            selectinload(ServiceRequest.table),
            selectinload(ServiceRequest.branch),
        )
        .where(
            ServiceRequest.status == ServiceRequestStatus.PENDING,
            ServiceRequest.is_escalated.is_(False),
            ServiceRequest.created_at <= cutoff_time,
        )
        .with_for_update(skip_locked=True)
        .order_by(ServiceRequest.created_at.asc())
    )

    result = await db.execute(stmt)
    overdue_requests: Sequence[ServiceRequest] = result.scalars().all()

    if not overdue_requests:
        return 0

    # 2. Mutate database records
    escalated_items_metadata: list[dict] = []
    for req in overdue_requests:
        req.is_escalated = True
        req.escalated_at = now
        req.updated_at = now

        created_utc = _ensure_utc(req.created_at) if req.created_at else now
        elapsed_seconds = int((now - created_utc).total_seconds())
        table_num = req.table.table_number if req.table else ""
        tenant_id = req.branch.tenant_id if req.branch else None

        escalated_items_metadata.append({
            "request_id": str(req.id),
            "branch_id": str(req.branch_id),
            "table_id": str(req.table_id),
            "table_number": table_num,
            "tenant_id": tenant_id,
            "type": req.request_type.value,
            "notes": req.note,
            "created_at": created_utc.isoformat(),
            "escalated_at": now.isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "branch_id_raw": req.branch_id,
            "req_id_raw": req.id,
        })

    # 3. Commit state changes to the database
    await db.commit()

    logger.warning(
        "SLA Monitor escalated %d overdue service request(s) exceeding %ds threshold",
        len(escalated_items_metadata),
        threshold_seconds,
    )

    # 4. Broadcast events strictly post-commit to both Runners and Admin channels
    for item in escalated_items_metadata:
        event_payload = {
            "request_id": item["request_id"],
            "branch_id": item["branch_id"],
            "table_id": item["table_id"],
            "table_number": item["table_number"],
            "type": item["type"],
            "notes": item["notes"],
            "created_at": item["created_at"],
            "escalated_at": item["escalated_at"],
            "elapsed_seconds": item["elapsed_seconds"],
        }

        # Targeted dual-channel broadcast: Runners + Branch Admin
        channels = [
            f"branch_{item['branch_id']}_runners",
            f"branch_{item['branch_id']}_admin",
        ]
        for channel in channels:
            try:
                await redis_pubsub.publish(
                    channel=channel,
                    event_type="SERVICE_REQUEST_ESCALATED",
                    data=event_payload,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to broadcast SERVICE_REQUEST_ESCALATED on %s: %s",
                    channel,
                    exc,
                )

        # 5. Audit Log escalation event
        if item["tenant_id"]:
            try:
                await AuditLogger.log(
                    tenant_id=item["tenant_id"],
                    action="SERVICE_REQUEST_ESCALATED",
                    resource_type="service_request",
                    resource_id=item["request_id"],
                    branch_id=item["branch_id_raw"],
                    actor_role="SYSTEM",
                    changes={
                        "is_escalated": True,
                        "escalated_at": item["escalated_at"],
                        "elapsed_seconds": item["elapsed_seconds"],
                    },
                )
            except Exception as exc:
                logger.debug("Failed to record SLA escalation audit log: %s", exc)

    return len(escalated_items_metadata)


class SLAMonitorWorker:
    """Periodic background worker monitoring table requests and escalating overdue items."""

    def __init__(
        self,
        interval_seconds: int = DEFAULT_MONITOR_INTERVAL_SECONDS,
        threshold_seconds: int = DEFAULT_SLA_THRESHOLD_SECONDS,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.threshold_seconds = threshold_seconds
        self._task: asyncio.Task | None = None
        self._is_running: bool = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def start(self) -> None:
        """Start the background monitoring loop task."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop(), name="sla_monitor_worker")
        logger.info(
            "SLAMonitorWorker started (interval=%ds, threshold=%ds)",
            self.interval_seconds,
            self.threshold_seconds,
        )

    async def stop(self) -> None:
        """Gracefully stop the background monitoring loop, cleanly catching CancelledError."""
        if not self._is_running:
            return
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Error awaiting SLAMonitorWorker task completion: %s", exc)
            finally:
                self._task = None
        logger.info("SLAMonitorWorker stopped cleanly.")

    async def _loop(self) -> None:
        """Continuous execution loop with error recovery and cancellation handling."""
        from app.core.database import async_session_factory

        while self._is_running:
            try:
                async with async_session_factory() as db:
                    escalated = await check_and_escalate_overdue_requests(
                        db=db,
                        threshold_seconds=self.threshold_seconds,
                    )
                    if escalated > 0:
                        logger.info("SLAMonitorWorker tick escalated %d request(s)", escalated)
            except asyncio.CancelledError:
                logger.debug("SLAMonitorWorker loop received cancellation signal")
                break
            except Exception as exc:
                logger.error("Unexpected error in SLAMonitorWorker loop: %s", exc)

            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                logger.debug("SLAMonitorWorker sleep interrupted by cancellation")
                break


# Global singleton instance
sla_monitor = SLAMonitorWorker()
