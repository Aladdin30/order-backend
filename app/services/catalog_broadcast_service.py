"""Catalog Real-Time Broadcast Service (Task BE-3.3).

Dispatches live 86 availability toggle events across scoped Redis Pub/Sub channels
to instantly synchronize catalog state across connected guest browsers and staff tablets.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.core.redis_pubsub import redis_pubsub

logger = logging.getLogger("app.services.catalog_broadcast_service")


async def broadcast_item_availability_change(
    branch_id: UUID,
    item_id: UUID,
    is_available: bool,
) -> None:
    """Broadcast real-time item 86 availability changes to branch catalog channel."""
    channel = f"branch_{branch_id}_catalog"
    event_type = "ITEM_AVAILABILITY_CHANGED"
    data = {
        "entity_type": "ITEM",
        "entity_id": str(item_id),
        "item_id": str(item_id),
        "is_available": is_available,
        "branch_id": str(branch_id),
    }
    try:
        await redis_pubsub.publish(channel=channel, event_type=event_type, data=data)
        logger.info(
            "Broadcast item availability change: item %s -> %s on %s",
            item_id,
            is_available,
            channel,
        )
    except Exception as exc:
        logger.warning(
            "Failed to broadcast item availability change for item %s: %s",
            item_id,
            exc,
        )


async def broadcast_modifier_availability_change(
    branch_id: UUID,
    modifier_option_id: UUID,
    group_id: UUID,
    is_available: bool,
) -> None:
    """Broadcast real-time modifier option 86 availability changes to branch catalog channel."""
    channel = f"branch_{branch_id}_catalog"
    event_type = "ITEM_AVAILABILITY_CHANGED"
    data = {
        "entity_type": "MODIFIER_OPTION",
        "entity_id": str(modifier_option_id),
        "group_id": str(group_id),
        "is_available": is_available,
        "branch_id": str(branch_id),
    }
    try:
        await redis_pubsub.publish(channel=channel, event_type=event_type, data=data)
        logger.info(
            "Broadcast modifier option availability change: option %s -> %s on %s",
            modifier_option_id,
            is_available,
            channel,
        )
    except Exception as exc:
        logger.warning(
            "Failed to broadcast modifier option availability change for option %s: %s",
            modifier_option_id,
            exc,
        )
