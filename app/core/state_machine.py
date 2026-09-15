"""Deterministic Order Finite State Machine (FSM) and lifecycle transition rules."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.models.enums import OrderStatus, TableStatus, UserRole


class OrderStateMachine:
    """Authoritative transition engine governing order lifecycles and role-based guards."""

    # Explicit legal transition graph
    VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.DRAFT: {
            OrderStatus.SUBMITTED,
            OrderStatus.PENDING_STAFF_CONFIRMATION,
            OrderStatus.CANCELLED,
        },
        OrderStatus.PENDING_STAFF_CONFIRMATION: {
            OrderStatus.SUBMITTED,
            OrderStatus.CANCELLED,
        },
        OrderStatus.SUBMITTED: {
            OrderStatus.PREPARING,
            OrderStatus.CANCELLED,
        },
        OrderStatus.PREPARING: {
            OrderStatus.READY,
            OrderStatus.CANCELLED,
        },
        OrderStatus.READY: {
            OrderStatus.DELIVERED,
        },
        OrderStatus.DELIVERED: {
            OrderStatus.CLOSED,
        },
        OrderStatus.CLOSED: set(),
        OrderStatus.CANCELLED: set(),
    }

    # Roles authorized to cancel orders once cooking has started
    ADMIN_CANCELLATION_ROLES: set[UserRole] = {
        UserRole.BRANCH_ADMIN,
        UserRole.REGIONAL_MANAGER,
        UserRole.SUPER_ADMIN,
    }

    @classmethod
    def assert_valid_transition(
        cls,
        current_status: OrderStatus,
        target_status: OrderStatus,
    ) -> None:
        """Validate whether current -> target status transition is structurally legal.

        Raises:
            HTTPException(409): If the transition is not in the legal FSM transition matrix.
        """
        allowed_targets = cls.VALID_TRANSITIONS.get(current_status, set())
        if target_status not in allowed_targets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_STATE_TRANSITION",
            )

    @classmethod
    def assert_can_cancel(
        cls,
        current_status: OrderStatus,
        actor_role: UserRole | str | None,
        reason: str | None = None,
    ) -> None:
        """Enforce strict cancellation guard rules across customer and staff actors.

        Rules:
        1. Customers can only cancel orders in DRAFT or PENDING_STAFF_CONFIRMATION.
        2. Once SUBMITTED or PREPARING, cancellation is strictly restricted to BRANCH_ADMIN+
           and requires a non-empty audit reason.
        3. Once READY, DELIVERED, or CLOSED, cancellation is entirely forbidden (409).
        """
        # Terminal or late-stage statuses cannot be cancelled under any role
        if current_status in (OrderStatus.READY, OrderStatus.DELIVERED, OrderStatus.CLOSED, OrderStatus.CANCELLED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="INVALID_STATE_TRANSITION",
            )

        is_guest = actor_role is None or actor_role == "GUEST"

        # Customer / Guest cancellation
        if is_guest:
            if current_status not in (OrderStatus.DRAFT, OrderStatus.PENDING_STAFF_CONFIRMATION):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CANCELLATION_RESTRICTED_TO_STAFF",
                )
            return

        # Staff cancellation
        staff_role = UserRole(actor_role) if isinstance(actor_role, str) else actor_role

        if current_status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            if staff_role not in cls.ADMIN_CANCELLATION_ROLES:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CANCELLATION_RESTRICTED_TO_STAFF",
                )
            if not reason or not reason.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="CANCELLATION_REASON_REQUIRED",
                )

    @classmethod
    def get_table_status_for_order_transition(
        cls,
        target_status: OrderStatus,
        has_other_active_orders: bool = False,
    ) -> TableStatus | None:
        """Determine corresponding Table status synchronization for an order transition."""
        if target_status in (OrderStatus.SUBMITTED, OrderStatus.PREPARING):
            return TableStatus.AWAITING_FOOD
        if target_status == OrderStatus.DELIVERED:
            return TableStatus.EATING
        if target_status == OrderStatus.CLOSED:
            if not has_other_active_orders:
                return TableStatus.AVAILABLE
            return None
        if target_status == OrderStatus.CANCELLED:
            if not has_other_active_orders:
                return TableStatus.BROWSING
            return None
        return None
