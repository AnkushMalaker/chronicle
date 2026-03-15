"""
Application factory for Chronicle backend.

Creates and configures the FastAPI application with all routers, middleware,
and service initializations.
"""

import asyncio
import logging
import time
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
from advanced_omi_backend.client_manager import get_client_manager
from advanced_omi_backend.middleware.app_middleware import setup_middleware
from advanced_omi_backend.routers.api_router import router as api_router
from advanced_omi_backend.routers.modules.health_routes import router as health_router
from advanced_omi_backend.routers.modules.websocket_routes import (
    router as websocket_router,
)
from advanced_omi_backend.services.audio_service import get_audio_stream_service
from advanced_omi_backend.services.memory import (
    get_memory_service,
    shutdown_memory_service,
)
from advanced_omi_backend.task_manager import get_task_manager, init_task_manager
from advanced_omi_backend.users import (
    User,
    UserRead,
    UserUpdate,
    register_client_to_user,
)

logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")


async def initialize_openmemory_user() -> None:
    """Initialize and register OpenMemory user if using OpenMemory MCP provider.

    This function:
    - Checks if OpenMemory MCP is configured as the memory provider
    - Registers the configured user with OpenMemory server
    - Creates a test memory and deletes it to trigger user creation
    - Logs success or warning if OpenMemory is not reachable
    """
    from advanced_omi_backend.services.memory.config import (
        MemoryProvider,
        build_memory_config_from_env,
    )

    memory_provider_config = build_memory_config_from_env()

    if memory_provider_config.memory_provider != MemoryProvider.OPENMEMORY_MCP:
        return

    try:
        from advanced_omi_backend.services.memory.providers.mcp_client import MCPClient

        # Get configured user_id and server_url
        openmemory_config = memory_provider_config.openmemory_config
        user_id = (
            openmemory_config.get("user_id", "openmemory")
            if openmemory_config
            else "openmemory"
        )
        server_url = (
            openmemory_config.get("server_url", "http://host.docker.internal:8765")
            if openmemory_config
            else "http://host.docker.internal:8765"
        )
        client_name = (
            openmemory_config.get("client_name", "chronicle")
            if openmemory_config
            else "chronicle"
        )

        application_logger.info(
            f"Registering OpenMemory user: {user_id} at {server_url}"
        )

        # Make a lightweight registration call (create and delete dummy memory)
        async with MCPClient(
            server_url=server_url, client_name=client_name, user_id=user_id
        ) as client:
            # Test connection first
            is_connected = await client.test_connection()
            if is_connected:
                # Create and immediately delete a dummy memory to trigger user creation
                memory_ids = await client.add_memories(
                    "Chronicle initialization - user registration test"
                )
                if memory_ids:
                    # Delete the test memory
                    await client.delete_memory(memory_ids[0])
                application_logger.info(f"✅ Registered OpenMemory user: {user_id}")
            else:
                application_logger.warning(
                    f"⚠️  OpenMemory MCP not reachable at {server_url}"
                )
                application_logger.info(
                    "User will be auto-created on first memory operation"
                )
    except Exception as e:
        application_logger.warning(f"⚠️  Could not register OpenMemory user: {e}")
        application_logger.info("User will be auto-created on first memory operation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan events."""
    config = get_app_config()
    startup_start = time.monotonic()

    # Startup
    application_logger.info("Starting application...")

    # ── Phase 1 (sequential — dependencies) ──────────────────────────
    phase_start = time.monotonic()

    # Initialize Beanie for all document models
    try:
        from beanie import init_beanie

        from advanced_omi_backend.models.annotation import Annotation
        from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
        from advanced_omi_backend.models.conversation import Conversation
        from advanced_omi_backend.models.user import User
        from advanced_omi_backend.models.waveform import WaveformData

        await init_beanie(
            database=config.db,
            document_models=[
                User,
                Conversation,
                AudioChunkDocument,
                WaveformData,
                Annotation,
            ],
        )
        application_logger.info("Beanie initialized for all document models")
    except Exception as e:
        application_logger.error(f"Failed to initialize Beanie: {e}")
        raise

    # Create admin user if needed (requires Beanie)
    try:
        await create_admin_user_if_needed()
    except Exception as e:
        application_logger.error(f"Failed to create admin user: {e}")

    application_logger.info(
        f"Phase 1 (Beanie + admin) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 2 (parallel — all independent) ─────────────────────────
    phase_start = time.monotonic()

    async def _init_redis_rq():
        try:
            from advanced_omi_backend.controllers.queue_controller import redis_conn

            redis_conn.ping()
            application_logger.info("Redis connection established for RQ")
        except Exception as e:
            application_logger.error(f"Failed to connect to Redis for RQ: {e}")
            application_logger.warning(
                "RQ queue system will not be available - check Redis connection"
            )

    async def _init_task_manager():
        try:
            tm = init_task_manager()
            await tm.start()
            application_logger.info("BackgroundTaskManager initialized and started")
        except Exception as e:
            application_logger.error(f"Failed to initialize task manager: {e}")
            raise  # Task manager is essential

    async def _init_client_manager():
        get_client_manager()
        application_logger.info("ClientManager initialized")

    async def _init_otel():
        try:
            from advanced_omi_backend.observability.otel_setup import init_otel

            init_otel()
        except Exception as e:
            application_logger.warning(f"OTEL initialization skipped: {e}")

    async def _init_prompt_registry():
        try:
            from advanced_omi_backend.prompt_defaults import register_all_defaults
            from advanced_omi_backend.prompt_registry import get_prompt_registry

            registry = get_prompt_registry()
            register_all_defaults(registry)
            application_logger.info(
                f"Prompt registry initialized with {len(registry._defaults)} defaults"
            )
        except Exception as e:
            application_logger.warning(f"Prompt registry initialization failed: {e}")

    await asyncio.gather(
        _init_redis_rq(),
        _init_task_manager(),
        _init_client_manager(),
        _init_otel(),
        _init_prompt_registry(),
    )

    application_logger.info(
        f"Phase 2 (Redis/TaskMgr/ClientMgr/OTEL/Prompts) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 3 (parallel — OTEL done, safe for LLM patching) ────────
    phase_start = time.monotonic()

    async def _init_llm_client():
        try:
            from advanced_omi_backend.llm_client import get_llm_client

            get_llm_client()
            application_logger.info("LLM client initialized from config.yml")
        except Exception as e:
            application_logger.warning(f"LLM client initialization deferred: {e}")

    async def _init_audio_stream_service():
        try:
            audio_service = get_audio_stream_service()
            await audio_service.connect()
            application_logger.info("Audio stream service connected to Redis Streams")
        except Exception as e:
            application_logger.error(f"Failed to connect audio stream service: {e}")
            application_logger.warning(
                "Redis Streams audio processing will not be available"
            )

    async def _init_redis_audio_producer():
        try:
            app.state.redis_audio_stream = await redis.from_url(
                config.redis_url, encoding="utf-8", decode_responses=False
            )
            from advanced_omi_backend.services.audio_stream import AudioStreamProducer

            app.state.audio_stream_producer = AudioStreamProducer(
                app.state.redis_audio_stream
            )
            application_logger.info(
                "Redis client for audio streaming producer initialized"
            )

            from advanced_omi_backend.client_manager import (
                initialize_redis_for_client_manager,
            )

            initialize_redis_for_client_manager(config.redis_url)
        except Exception as e:
            application_logger.error(
                f"Failed to initialize Redis client for audio streaming: {e}",
                exc_info=True,
            )
            application_logger.warning("Audio streaming producer will not be available")

    async def _deferred_prompt_seed():
        """Seed prompts into Langfuse with retry backoff."""
        try:
            from advanced_omi_backend.prompt_registry import get_prompt_registry

            registry = get_prompt_registry()
        except Exception:
            return

        backoff_delays = [0, 2, 4, 8, 16, 32]
        for delay in backoff_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                await registry.seed_prompts()
                application_logger.info("Prompt seeding to Langfuse completed")
                return
            except Exception as e:
                application_logger.debug(
                    f"Prompt seeding attempt failed (next retry in {delay}s): {e}"
                )
        application_logger.warning(
            "Prompt seeding to Langfuse failed after all retries"
        )

    await asyncio.gather(
        _init_llm_client(),
        _init_audio_stream_service(),
        _init_redis_audio_producer(),
    )

    # Launch deferred prompt seeding as a fire-and-forget background task
    asyncio.create_task(_deferred_prompt_seed())

    application_logger.info(
        f"Phase 3 (LLM/AudioStream/RedisProducer) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 4 (parallel — all independent) ─────────────────────────
    phase_start = time.monotonic()

    application_logger.info(
        "Memory service will be initialized on first use (lazy loading)"
    )

    async def _init_openmemory():
        await initialize_openmemory_user()

    async def _init_cron_scheduler():
        try:
            from advanced_omi_backend.cron_scheduler import (
                get_scheduler,
                register_cron_job,
            )
            from advanced_omi_backend.workers.annotation_jobs import (
                surface_error_suggestions,
            )
            from advanced_omi_backend.workers.finetuning_jobs import (
                run_asr_finetuning_job,
                run_asr_jargon_extraction_job,
                run_speaker_finetuning_job,
            )
            from advanced_omi_backend.workers.prompt_optimization_jobs import (
                run_prompt_optimization_job,
            )

            register_cron_job("speaker_finetuning", run_speaker_finetuning_job)
            register_cron_job("asr_finetuning", run_asr_finetuning_job)
            register_cron_job("asr_jargon_extraction", run_asr_jargon_extraction_job)
            register_cron_job("prompt_optimization", run_prompt_optimization_job)
            register_cron_job("annotation_suggestions", surface_error_suggestions)

            scheduler = get_scheduler()
            await scheduler.start()
            application_logger.info("Cron scheduler started")
        except Exception as e:
            application_logger.warning(f"Cron scheduler failed to start: {e}")

    async def _init_plugins():
        try:
            from advanced_omi_backend.services.plugin_service import (
                init_plugin_router,
                set_plugin_router,
            )

            plugin_router = init_plugin_router()

            if plugin_router:
                for plugin_id, plugin in plugin_router.plugins.items():
                    if plugin.enabled:
                        try:
                            await plugin.initialize()
                            plugin_router.mark_plugin_initialized(plugin_id)
                            application_logger.info(f"Plugin '{plugin_id}' initialized")
                        except Exception as e:
                            plugin_router.mark_plugin_failed(plugin_id, str(e))
                            application_logger.error(
                                f"Failed to initialize plugin '{plugin_id}': {e}",
                                exc_info=True,
                            )

                health = plugin_router.get_health_summary()
                application_logger.info(
                    f"Plugins initialized: {health['initialized']}/{health['total']} active"
                    + (f", {health['failed']} failed" if health["failed"] else "")
                )

                app.state.plugin_router = plugin_router
                set_plugin_router(plugin_router)
            else:
                application_logger.info("No plugins configured")
                app.state.plugin_router = None

        except Exception as e:
            application_logger.error(
                f"Failed to initialize plugin system: {e}", exc_info=True
            )
            app.state.plugin_router = None

    await asyncio.gather(
        _init_openmemory(),
        _init_cron_scheduler(),
        _init_plugins(),
    )

    application_logger.info(
        f"Phase 4 (OpenMemory/Cron/Plugins) completed in {time.monotonic() - phase_start:.2f}s"
    )

    total_startup = time.monotonic() - startup_start
    application_logger.info(
        f"Application ready in {total_startup:.2f}s - using application-level processing architecture."
    )

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
                from advanced_omi_backend.controllers.websocket_controller import (
                    cleanup_client_state,
                )

                await cleanup_client_state(client_id)
            except Exception as e:
                application_logger.error(f"Error cleaning up client {client_id}: {e}")

        # Shutdown BackgroundTaskManager
        try:
            task_mgr = get_task_manager()
            await task_mgr.shutdown()
            application_logger.info("BackgroundTaskManager shut down")
        except RuntimeError:
            pass  # Never initialized
        except Exception as e:
            application_logger.error(f"Error shutting down task manager: {e}")

        # RQ workers shut down automatically when process ends
        # No special cleanup needed for Redis connections

        # Shutdown audio stream service
        try:
            audio_service = get_audio_stream_service()
            await audio_service.disconnect()
            application_logger.info("Audio stream service disconnected")
        except Exception as e:
            application_logger.error(f"Error disconnecting audio stream service: {e}")

        # Close Redis client for audio streaming producer
        try:
            if (
                hasattr(app.state, "redis_audio_stream")
                and app.state.redis_audio_stream
            ):
                await app.state.redis_audio_stream.close()
                application_logger.info(
                    "Redis client for audio streaming producer closed"
                )
        except Exception as e:
            application_logger.error(f"Error closing Redis audio streaming client: {e}")

        # Stop metrics collection and save final report
        application_logger.info("Metrics collection stopped")

        # Shutdown plugins
        try:
            from advanced_omi_backend.services.plugin_service import (
                cleanup_plugin_router,
            )

            await cleanup_plugin_router()
            application_logger.info("Plugins shut down")
        except Exception as e:
            application_logger.error(f"Error shutting down plugins: {e}")

        # Shutdown cron scheduler
        try:
            from advanced_omi_backend.cron_scheduler import get_scheduler

            scheduler = get_scheduler()
            await scheduler.stop()
            application_logger.info("Cron scheduler stopped")
        except Exception as e:
            application_logger.error(f"Error stopping cron scheduler: {e}")

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

    logger.info(
        "FastAPI application created with all routers and middleware configured"
    )

    return app
