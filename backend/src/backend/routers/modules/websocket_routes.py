"""
WebSocket routes for Chronicle backend.

This module handles WebSocket connections for audio streaming.
"""

from fastapi import APIRouter, WebSocket

from backend.controllers.audio_v2_controller import handle_audio_v2_websocket

# Create router
router = APIRouter(tags=["websocket"])


@router.websocket("/ws/audio")
async def audio_v2_endpoint(ws: WebSocket):
    """Chronicle's typed Opus-only audio protocol."""

    await handle_audio_v2_websocket(ws)
