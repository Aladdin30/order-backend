"""In-memory ASGI WebSocket Connection Manager.

Maintains per-worker active connection mappings, manages subscriptions across
scoped channels, ensures thread/concurrency safety via asyncio.Lock, and handles
broken pipe detection and clean eviction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

logger = logging.getLogger("app.core.websocket_manager")


class WebSocketManager:
    """Manages local WebSocket connections and channel subscriptions per ASGI worker."""

    def __init__(self) -> None:
        # Channel name -> Set of connected WebSockets
        self._subscriptions: dict[str, set[WebSocket]] = {}
        # WebSocket -> Set of subscribed channel names (for O(1) disconnect cleanup)
        self._socket_channels: dict[WebSocket, set[str]] = {}
        # Concurrency safety lock for mutating registries
        self._lock: asyncio.Lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, channels: list[str] | set[str]) -> None:
        """Accept handshake if not already accepted, and register channel subscriptions."""
        if websocket.client_state == WebSocketState.CONNECTING:
            await websocket.accept()

        channel_set = {ch.strip() for ch in channels if ch and ch.strip()}
        if not channel_set:
            return

        async with self._lock:
            # Register in reverse lookup map
            if websocket not in self._socket_channels:
                self._socket_channels[websocket] = set()
            self._socket_channels[websocket].update(channel_set)

            # Register in channel subscriptions
            for ch in channel_set:
                if ch not in self._subscriptions:
                    self._subscriptions[ch] = set()
                self._subscriptions[ch].add(websocket)

        logger.debug(
            "WebSocket connected and subscribed to channels: %s (total active channels: %d)",
            channel_set,
            len(self._subscriptions),
        )

    async def disconnect(self, websocket: WebSocket) -> set[str]:
        """Evict the socket from all channels and prune empty channel keys.

        Returns:
            The set of channels the socket was subscribed to before eviction.
        """
        async with self._lock:
            subscribed_channels = self._socket_channels.pop(websocket, set())
            for ch in subscribed_channels:
                sockets = self._subscriptions.get(ch)
                if sockets is not None:
                    sockets.discard(websocket)
                    if not sockets:
                        del self._subscriptions[ch]

        logger.debug(
            "WebSocket evicted from channels: %s (remaining active channels: %d)",
            subscribed_channels,
            len(self._subscriptions),
        )
        return subscribed_channels

    async def broadcast_local(self, channel: str, message: dict[str, Any]) -> None:
        """Deliver parsed JSON payload to all local sockets subscribed to the channel.

        Detects broken pipes/client drops and automatically triggers eviction.
        """
        async with self._lock:
            target_sockets = list(self._subscriptions.get(channel, set()))

        if not target_sockets:
            return

        async def _send_safe(ws: WebSocket) -> None:
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning(
                    "WebSocket client drop or broken pipe detected during broadcast on channel %s: %s",
                    channel,
                    exc,
                )
                await self.disconnect(ws)

        await asyncio.gather(*[_send_safe(s) for s in target_sockets], return_exceptions=True)

    def is_connected(self, websocket: WebSocket) -> bool:
        """Return whether the WebSocket is currently registered."""
        return websocket in self._socket_channels

    def get_socket_channels(self, websocket: WebSocket) -> set[str]:
        """Return the set of channels subscribed by the given WebSocket."""
        return set(self._socket_channels.get(websocket, set()))

    def get_channel_subscribers_count(self, channel: str) -> int:
        """Return the number of local sockets subscribed to the given channel."""
        return len(self._subscriptions.get(channel, set()))

    def get_active_channels(self) -> set[str]:
        """Return all active channel names with at least one subscriber."""
        return set(self._subscriptions.keys())


# Singleton instance shared across the ASGI worker process
ws_manager = WebSocketManager()
