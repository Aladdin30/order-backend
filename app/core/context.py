"""SecurityContext representation and per-request authorization helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.auth import User


@dataclass(frozen=True)
class SecurityContext:
    """Immutable security context encapsulating authenticated user state and branch scoping."""

    user: User
    tenant_id: uuid.UUID
    role: UserRole
    allowed_branch_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @property
    def is_super_admin(self) -> bool:
        """Indicate whether the actor possesses tenant-wide administrative authority."""
        return self.role == UserRole.SUPER_ADMIN

    def can_access_branch(self, branch_id: uuid.UUID) -> bool:
        """Determine whether the actor has access rights to the specified branch."""
        if self.is_super_admin:
            return True
        return branch_id in self.allowed_branch_ids

    def assert_branch_access(self, branch_id: uuid.UUID) -> None:
        """Assert branch permission or raise HTTP 403 Forbidden."""
        if not self.can_access_branch(branch_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to branch '{branch_id}' is forbidden for role '{self.role}'.",
            )

    def assert_roles(self, allowed_roles: set[UserRole] | list[UserRole]) -> None:
        """Assert user has one of the allowed roles or raise HTTP 403 Forbidden."""
        if self.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{self.role}' is not authorized to perform this operation.",
            )
