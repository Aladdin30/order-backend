"""Service layer for dynamic branch kitchen station catalog management."""

from __future__ import annotations

import logging
import uuid
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Item
from app.models.kitchen_station import KitchenStation
from app.schemas.kitchen_station import (
    CreateKitchenStationRequest,
    KitchenStationResponse,
    UpdateKitchenStationRequest,
)

logger = logging.getLogger("app.services.kitchen_station_service")

# Baseline default operational stations seeded automatically per branch
DEFAULT_STATIONS_CONFIG = [
    {
        "code": "HOT_KITCHEN",
        "name": {"en": "Hot Kitchen", "ar": "المطبخ الساخن"},
    },
    {
        "code": "COLD_KITCHEN",
        "name": {"en": "Cold Kitchen", "ar": "المطبخ البارد"},
    },
    {
        "code": "BEVERAGE",
        "name": {"en": "Beverage & Bar", "ar": "المشروبات والبار"},
    },
]


class KitchenStationService:
    """Business logic for branch kitchen station creation, listing, updating, and deactivation."""

    @classmethod
    async def ensure_default_stations(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> list[KitchenStation]:
        """Verify baseline default stations exist for the branch, auto-seeding if missing."""
        stmt = select(KitchenStation).where(KitchenStation.branch_id == branch_id)
        res = await db.execute(stmt)
        existing = list(res.scalars().all())
        existing_codes = {s.code for s in existing}

        seeded: list[KitchenStation] = list(existing)
        new_records = False

        for conf in DEFAULT_STATIONS_CONFIG:
            if conf["code"] not in existing_codes:
                station = KitchenStation(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    name=conf["name"],
                    code=conf["code"],
                    is_active=True,
                )
                db.add(station)
                seeded.append(station)
                new_records = True

        if new_records:
            await db.flush()
            await db.commit()
            logger.info("Auto-seeded default kitchen stations for branch %s", branch_id)

        return seeded

    @classmethod
    async def create_station(
        cls,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        branch_id: uuid.UUID,
        payload: CreateKitchenStationRequest,
    ) -> KitchenStationResponse:
        """Create a custom branch kitchen station with unique uppercase code guard."""
        # Ensure default stations exist first
        await cls.ensure_default_stations(db, branch_id=branch_id, tenant_id=tenant_id)

        code_upper = payload.code.strip().upper()

        # Uniqueness guard: ("branch_id", "code")
        stmt_dup = select(KitchenStation).where(
            KitchenStation.branch_id == branch_id,
            KitchenStation.code == code_upper,
        )
        res_dup = await db.execute(stmt_dup)
        if res_dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Station code '{code_upper}' already exists in this branch.",
            )

        station = KitchenStation(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            branch_id=branch_id,
            name=payload.name,
            code=code_upper,
            is_active=payload.is_active,
        )
        db.add(station)
        await db.commit()
        await db.refresh(station)

        logger.info("Created custom kitchen station %s (%s) for branch %s", station.code, station.id, branch_id)
        return KitchenStationResponse.model_validate(station)

    @classmethod
    async def list_stations(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        include_inactive: bool = False,
    ) -> list[KitchenStationResponse]:
        """Return all kitchen stations for the specified branch, auto-seeding defaults if needed."""
        await cls.ensure_default_stations(db, branch_id=branch_id, tenant_id=tenant_id)

        stmt = select(KitchenStation).where(KitchenStation.branch_id == branch_id)
        if not include_inactive:
            stmt = stmt.where(KitchenStation.is_active.is_(True))
        stmt = stmt.order_by(KitchenStation.created_at.asc())

        res = await db.execute(stmt)
        stations = res.scalars().all()
        return [KitchenStationResponse.model_validate(s) for s in stations]

    @classmethod
    async def get_station_by_id(
        cls,
        db: AsyncSession,
        station_id: uuid.UUID,
        branch_id: uuid.UUID,
    ) -> KitchenStation:
        """Fetch a station model by ID with branch scoping."""
        stmt = select(KitchenStation).where(
            KitchenStation.id == station_id,
            KitchenStation.branch_id == branch_id,
        )
        res = await db.execute(stmt)
        station = res.scalar_one_or_none()
        if station is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="KITCHEN_STATION_NOT_FOUND",
            )
        return station

    @classmethod
    async def update_station(
        cls,
        db: AsyncSession,
        station_id: uuid.UUID,
        branch_id: uuid.UUID,
        payload: UpdateKitchenStationRequest,
    ) -> KitchenStationResponse:
        """Update station attributes (name, is_active). Deactivation validates active references."""
        station = await cls.get_station_by_id(db, station_id=station_id, branch_id=branch_id)

        # If deactivating, guard against active catalog usage
        if payload.is_active is False and station.is_active:
            await cls._assert_no_active_references(db, station_id=station.id)

        if payload.name is not None:
            station.name = payload.name
        if payload.is_active is not None:
            station.is_active = payload.is_active

        await db.commit()
        await db.refresh(station)
        return KitchenStationResponse.model_validate(station)

    @classmethod
    async def delete_station(
        cls,
        db: AsyncSession,
        station_id: uuid.UUID,
        branch_id: uuid.UUID,
        soft: bool = True,
    ) -> None:
        """Soft-deactivate (default) or delete a station, strictly blocking with 409 if assigned to active items/categories."""
        station = await cls.get_station_by_id(db, station_id=station_id, branch_id=branch_id)
        await cls._assert_no_active_references(db, station_id=station.id)

        if soft:
            station.is_active = False
        else:
            await db.delete(station)
        await db.commit()
        logger.info("Deactivated/deleted kitchen station %s (%s, soft=%s)", station.code, station.id, soft)

    @classmethod
    async def _assert_no_active_references(
        cls,
        db: AsyncSession,
        station_id: uuid.UUID,
    ) -> None:
        """Raise 409 Conflict if this station is assigned to any active menu items or categories."""
        # Check active items
        item_stmt = select(func.count(Item.id)).where(
            Item.station_id == station_id,
            Item.is_available.is_(True),
        )
        active_items = (await db.execute(item_stmt)).scalar() or 0
        if active_items > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate station: {active_items} active menu item(s) are currently assigned to it.",
            )

        # Check active categories
        cat_stmt = select(func.count(Category.id)).where(
            Category.station_id == station_id,
            Category.is_active.is_(True),
        )
        active_cats = (await db.execute(cat_stmt)).scalar() or 0
        if active_cats > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate station: {active_cats} active category(s) are currently assigned to it.",
            )
