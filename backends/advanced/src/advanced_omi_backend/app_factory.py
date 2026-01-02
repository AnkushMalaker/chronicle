"""
Application factory for Chronicle backend.

Creates and configures the FastAPI application with all routers, middleware,
and service initializations.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from advanced_omi_backend.app_config import get_app_config
from advanced_omi_backend.auth import (
    bearer_backend,
    cookie_backend,
    create_admin_user_if_needed,
    current_superuser,
    fastapi_users,
    websocket_auth,
)
from advanced_omi_backend.users import (
    User,
    UserRead,
    UserUpdate,
    register_client_to_user,
)
from advanced_omi_backend.client_manager import get_client_manager
from advanced_omi_backend.services.memory import get_memory_service, shutdown_memory_service
from advanced_omi_backend.middleware.app_middleware import setup_middleware
from advanced_omi_backend.routers.api_router import router as api_router
from advanced_omi_backend.routers.modules.health_routes import router as health_router
from advanced_omi_backend.routers.modules.websocket_routes import router as websocket_router
from advanced_omi_backend.services.audio_service import get_audio_stream_service
from advanced_omi_backend.task_manager import init_task_manager, get_task_manager

logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan events."""
    config = get_app_config()

    # Startup
    application_logger.info("Starting application...")

    # Initialize Beanie for all document models
    try:
        from beanie import init_beanie
        from advanced_omi_backend.models.conversation import Conversation
        from advanced_omi_backend.models.audio_file import AudioFile
        from advanced_omi_backend.models.user import User

        await init_beanie(
            database=config.db,
            document_models=[User, Conversation, AudioFile],
        )
        application_logger.info("Beanie initialized for all document models")
    except Exception:
        application_logger.exception(f"Failed to initialize Beanie")
        raise

    # Create admin user if needed
    try:
        await create_admin_user_if_needed()
    except Exception:
        application_logger.exception(f"Failed to create admin user")
        # Don't raise here as this is not critical for startup

    # Sync admin user with Mycelia OAuth (if using Mycelia memory provider)
    try:
        from advanced_omi_backend.services.mycelia_sync import sync_admin_on_startup
        await sync_admin_on_startup()
    except Exception:
        application_logger.exception(f"Failed to sync admin with Mycelia OAuth")
        # Don't raise here as this is not critical for startup

    # Initialize Redis connection for RQ
    try:
        from advanced_omi_backend.controllers.queue_controller import redis_conn
        redis_conn.ping()
        application_logger.info("Redis connection established for RQ")
        application_logger.info("RQ workers can be started with: rq worker transcription memory default")
    except Exception:
        application_logger.exception(f"Failed to connect to Redis for RQ")
        application_logger.warning("RQ queue system will not be available - check Redis connection")

    # Initialize audio stream service for Redis Streams
    try:
        audio_service = get_audio_stream_service()
        await audio_service.connect()
        application_logger.info("Audio stream service connected to Redis Streams")
        application_logger.info("Audio stream workers can be started with: python -m advanced_omi_backend.workers.audio_stream_worker")
    except Exception:
        application_logger.exception(f"Failed to connect audio stream service")
        application_logger.warning("Redis Streams audio processing will not be available")

    # Initialize Redis client for audio streaming producer (used by WebSocket handlers)
    try:
        app.state.redis_audio_stream = await redis.from_url(
            config.redis_url,
            encoding="utf-8",
            decode_responses=False
        )
        from advanced_omi_backend.services.audio_stream import AudioStreamProducer
        app.state.audio_stream_producer = AudioStreamProducer(app.state.redis_audio_stream)
        application_logger.info("✅ Redis client for audio streaming producer initialized")
    except Exception as e:
        application_logger.error(f"Failed to initialize Redis client for audio streaming: {e}", exc_info=True)
        application_logger.warning("Audio streaming producer will not be available")

    # Skip memory service pre-initialization to avoid blocking FastAPI startup
    # Memory service will be lazily initialized when first used
    application_logger.info("Memory service will be initialized on first use (lazy loading)")

    # SystemTracker is used for monitoring and debugging
    application_logger.info("Using SystemTracker for monitoring and debugging")

    application_logger.info("Application ready - using application-level processing architecture.")

    logger.info("App ready")
    try:
        yield
    finally:
        # Shutdown
        application_logger.info("Shutting down application...")

        # Clean up all active clients
        client_manager = get_client_manager()
        for client_id in client_manager.get_all_client_ids():
            try:
                from advanced_omi_backend.controllers.websocket_controller import cleanup_client_state
                await cleanup_client_state(client_id)
            except Exception:
                application_logger.exception(f"Error cleaning up client {client_id}")

        # RQ workers shut down automatically when process ends
        # No special cleanup needed for Redis connections

        # Shutdown audio stream service
        try:
            audio_service = get_audio_stream_service()
            await audio_service.disconnect()
            application_logger.info("Audio stream service disconnected")
        except Exception:
            application_logger.exception(f"Error disconnecting audio stream service")

        # Close Redis client for audio streaming producer
        try:
            if hasattr(app.state, 'redis_audio_stream') and app.state.redis_audio_stream:
                await app.state.redis_audio_stream.close()
                application_logger.info("Redis client for audio streaming producer closed")
        except Exception:
            application_logger.exception(f"Error closing Redis audio streaming client")

        # Stop metrics collection and save final report
        application_logger.info("Metrics collection stopped")

        # Shutdown memory service and speaker service
        shutdown_memory_service()
        application_logger.info("Memory and speaker services shut down.")

        application_logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Create FastAPI application with lifespan management
    app = FastAPI(lifespan=lifespan)

    # Set up middleware (CORS, exception handlers)
    setup_middleware(app)

    # Include all routers
    app.include_router(api_router)

    # Add health check router at root level (not under /api prefix)
    app.include_router(health_router)

    # Add WebSocket router at root level (not under /api prefix)
    app.include_router(websocket_router)

   # Add authentication routers
    app.include_router(
        fastapi_users.get_auth_router(cookie_backend),
        prefix="/auth/cookie",
        tags=["auth"],
    )
    app.include_router(
        fastapi_users.get_auth_router(bearer_backend),
        prefix="/auth/jwt",
        tags=["auth"],
    )

    # Add users router for /users/me and other user endpoints
    app.include_router(
        fastapi_users.get_users_router(UserRead, UserUpdate),
        prefix="/users",
        tags=["users"],
    )

    # Mount static files LAST (mounts are catch-all patterns)
    CHUNK_DIR = Path("/app/audio_chunks")
    app.mount("/audio", StaticFiles(directory=CHUNK_DIR), name="audio")

    logger.info("FastAPI application created with all routers and middleware configured")

    return app