"""
WebSocket routes for Chronicle backend.

This module handles WebSocket connections for audio streaming.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from advanced_omi_backend.controllers.websocket_controller import (
    handle_omi_websocket,
    handle_pcm_websocket,
)

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(tags=["websocket"])


@router.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket,
    codec: str = Query("pcm"),
    token: Optional[str] = Query(None),
    device_name: Optional[str] = Query(None),
):
    """
    WebSocket endpoint for audio streaming with multiple codec support.

    Args:
        codec: Audio codec (pcm, opus). Default: pcm
        token: JWT auth token
        device_name: Device identifier

    Examples:
        /ws?codec=pcm&token=xxx&device_name=laptop
        /ws?codec=opus&token=xxx&device_name=omi-device
    """
    # Validate and normalize codec
    codec = codec.lower()
    if codec not in ["pcm", "opus"]:
        logger.warning(f"Unsupported codec requested: {codec}")
        await ws.close(
            code=1008, reason=f"Unsupported codec: {codec}. Supported: pcm, opus"
        )
        return

    # Route to appropriate handler
    if codec == "opus":
        await handle_omi_websocket(ws, token, device_name)
    else:
        await handle_pcm_websocket(ws, token, device_name)
