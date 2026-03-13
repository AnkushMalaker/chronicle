#!/usr/bin/env python3
"""
Unified Omi-audio service

 * Accepts audio over a unified WebSocket endpoint (`/ws`) with codec parameter (pcm or opus).
 * Uses a central queue to decouple audio ingestion from processing.
 * A saver consumer buffers PCM and writes 30-second WAV chunks to `./data/audio_chunks/`.
 * A transcription consumer sends each chunk to a Wyoming ASR service.
 * The transcript is stored in **mem0** and MongoDB.

Refactored to use a modular architecture with proper separation of concerns:
- app_factory.py: FastAPI application creation and configuration
- app_config.py: Centralized configuration management
- middleware/app_middleware.py: CORS and exception handling
- routers/modules/: Organized route handlers
"""

import logging

import uvicorn

from advanced_omi_backend.app_factory import create_app

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("advanced-backend")

# Create FastAPI application using the app factory pattern
app = create_app()


if __name__ == "__main__":
    """Main entry point for running the application."""
    import os

    # Get port from environment or use default
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")

    logger.info(f"Starting server on {host}:{port}")

    # Run the application
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,  # Set to True for development
        access_log=False,  # Disabled - using custom RequestLoggingMiddleware instead
        log_level="info",
    )
