"""
Main API router for Chronicle backend.

This module aggregates all the functional router modules and provides
a single entry point for the API endpoints.
"""

import logging
import os

from fastapi import APIRouter

from .modules import (
    admin_router,
    annotation_router,
    audio_router,
    chat_router,
    client_router,
    conversation_router,
    finetuning_router,
    knowledge_graph_router,
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
router.include_router(admin_router)
router.include_router(annotation_router)
router.include_router(audio_router)
router.include_router(user_router)
router.include_router(chat_router)
router.include_router(client_router)
router.include_router(conversation_router)
router.include_router(finetuning_router)
router.include_router(knowledge_graph_router)
router.include_router(memory_router)
router.include_router(obsidian_router)
router.include_router(system_router)
router.include_router(queue_router)
router.include_router(
    health_router
)  # Also include under /api for frontend compatibility

# Conditionally include test routes (only in test environments)
if os.getenv("DEBUG_DIR"):
    try:
        from .modules.test_routes import router as test_router

        router.include_router(test_router)
        logger.info("✅ Test routes loaded (test environment detected)")
    except Exception as e:
        logger.error(f"Error loading test routes: {e}", exc_info=True)

logger.info("API router initialized with all sub-modules")
