"""
Main API router for Chronicle backend.

This module aggregates all the functional router modules and provides
a single entry point for the API endpoints.
"""

import logging

from fastapi import APIRouter

from .modules import (
    audio_router,
    chat_router,
    client_router,
    conversation_router,
    memory_router,
    obsidian_router,
    queue_router,
    system_router,
    user_router,
)
from .modules.health_routes import router as health_router

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

# Create main API router
router = APIRouter(prefix="/api", tags=["api"])

# Include all sub-routers
router.include_router(audio_router)
router.include_router(user_router)
router.include_router(chat_router)
router.include_router(client_router)
router.include_router(conversation_router)
router.include_router(memory_router)
router.include_router(obsidian_router)
router.include_router(system_router)
router.include_router(queue_router)
router.include_router(health_router)  # Also include under /api for frontend compatibility


logger.info("API router initialized with all sub-modules")
