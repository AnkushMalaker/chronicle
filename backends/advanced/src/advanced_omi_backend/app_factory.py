"""
Application factory for Chronicle backend.

Creates and configures the FastAPI application with all routers, middleware,
and service initializations.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from beanie import init_beanie
from fastapi import FastAPI

from advanced_omi_backend.app_config import get_app_config
from advanced_omi_backend.auth import (
    bearer_backend,
    cookie_backend,
    create_admin_user_if_needed,
    current_superuser,
    fastapi_users,
    websocket_auth,
)
from advanced_omi_backend.client_manager import (
    get_client_manager,
    initialize_redis_for_client_manager,
)
from advanced_omi_backend.controllers.data_audit_controller import run_auto_clean_cron
from advanced_omi_backend.controllers.queue_controller import redis_conn
from advanced_omi_backend.controllers.websocket_controller import cleanup_client_state
from advanced_omi_backend.cron_scheduler import get_scheduler, register_cron_job
from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.middleware.app_middleware import setup_middleware
from advanced_omi_backend.models.annotation import Annotation
from advanced_omi_backend.models.api_key import ApiKey
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    PairingCode,
)
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.system_event import SystemEvent
from advanced_omi_backend.models.timeline import (
    AudioEvidenceSpan,
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.models.waveform import WaveformData
from advanced_omi_backend.observability.otel_setup import init_otel
from advanced_omi_backend.prompt_defaults import register_all_defaults
from advanced_omi_backend.prompt_registry import get_prompt_registry
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.routers.api_router import router as api_router
from advanced_omi_backend.routers.modules.health_routes import router as health_router
from advanced_omi_backend.routers.modules.websocket_routes import (
    router as websocket_router,
)
from advanced_omi_backend.services.audio_service import get_audio_stream_service
from advanced_omi_backend.services.audio_stream import AudioStreamProducer
from advanced_omi_backend.services.device_audio_ingest import process_device_audio
from advanced_omi_backend.services.device_context import purge_screen_context
from advanced_omi_backend.services.immich_discovery import scan_immich_memories
from advanced_omi_backend.services.memory import (
    get_memory_service,
    shutdown_memory_service,
)
from advanced_omi_backend.services.memory.syncthing_audit import (
    start_syncthing_audit_listener,
)
from advanced_omi_backend.services.observability import run_event_ingest_drain
from advanced_omi_backend.services.observability.health_poller import run_health_poller
from advanced_omi_backend.services.person_photos import sync_person_photos
from advanced_omi_backend.services.plugin_service import (
    cleanup_plugin_router,
    init_plugin_router,
    initialize_plugins,
    run_plugin_recovery,
    set_plugin_router,
)
from advanced_omi_backend.services.reaper import run_reaper
from advanced_omi_backend.services.screenshots.describe import (
    process_screenshot_descriptions,
)
from advanced_omi_backend.services.screenshots.embed import (
    process_screenshot_embeddings,
)
from advanced_omi_backend.services.status_reconciler import (
    reconcile_conversation_statuses,
)
from advanced_omi_backend.services.timeline.discovery import (
    process_current_timeline_days,
)
from advanced_omi_backend.services.timeline.memory import process_episode_memory
from advanced_omi_backend.services.timeline.thumbnails import process_episode_thumbnails
from advanced_omi_backend.task_manager import get_task_manager, init_task_manager
from advanced_omi_backend.users import (
    User,
    UserRead,
    UserUpdate,
    register_client_to_user,
)
from advanced_omi_backend.workers.annotation_jobs import surface_error_suggestions
from advanced_omi_backend.workers.finetuning_jobs import (
    run_asr_finetuning_job,
    run_asr_jargon_extraction_job,
    run_speaker_finetuning_job,
)
from advanced_omi_backend.workers.prompt_optimization_jobs import (
    run_prompt_optimization_job,
)

logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")


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
        await init_beanie(
            database=config.db,
            document_models=[
                User,
                ApiKey,
                Conversation,
                AudioChunkDocument,
                WaveformData,
                Annotation,
                MemoryAuditEntry,
                SystemEvent,
                CaptureSource,
                PairingCode,
                DeviceInputItem,
                DeviceInputJob,
                AudioEvidenceSpan,
                TimelineAnalysisRun,
                TimelineEpisode,
                TimelineDay,
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

            init_otel()
        except Exception as e:
            application_logger.warning(f"OTEL initialization skipped: {e}")

    async def _init_prompt_registry():
        try:

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
            app.state.redis_audio_stream = create_async_redis(decode_responses=False)

            app.state.audio_stream_producer = AudioStreamProducer(
                app.state.redis_audio_stream
            )
            application_logger.info(
                "Redis client for audio streaming producer initialized"
            )

            initialize_redis_for_client_manager()
        except Exception as e:
            application_logger.error(
                f"Failed to initialize Redis client for audio streaming: {e}",
                exc_info=True,
            )
            application_logger.warning("Audio streaming producer will not be available")

    async def _deferred_prompt_seed():
        """Seed prompts into Langfuse with retry backoff."""
        try:

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

    async def _init_cron_scheduler():
        try:

            register_cron_job("speaker_finetuning", run_speaker_finetuning_job)
            register_cron_job("asr_finetuning", run_asr_finetuning_job)
            register_cron_job("asr_jargon_extraction", run_asr_jargon_extraction_job)
            register_cron_job("prompt_optimization", run_prompt_optimization_job)
            register_cron_job("annotation_suggestions", surface_error_suggestions)
            register_cron_job("auto_clean", run_auto_clean_cron)
            register_cron_job("immich_memories", scan_immich_memories)
            register_cron_job("person_photos", sync_person_photos)
            register_cron_job("device_audio_ingest", process_device_audio)
            register_cron_job("screen_context_retention", purge_screen_context)
            register_cron_job("timeline_analysis", process_current_timeline_days)
            register_cron_job("episode_thumbnails", process_episode_thumbnails)
            register_cron_job(
                "screenshot_descriptions", process_screenshot_descriptions
            )
            register_cron_job("screenshot_embeddings", process_screenshot_embeddings)
            register_cron_job("episode_memory", process_episode_memory)

            scheduler = get_scheduler()
            await scheduler.start()
            application_logger.info("Cron scheduler started")
        except Exception as e:
            application_logger.warning(f"Cron scheduler failed to start: {e}")

    async def _init_plugins():
        try:

            plugin_router = init_plugin_router()

            if plugin_router:
                await initialize_plugins(plugin_router)

                health = plugin_router.get_health_summary()
                application_logger.info(
                    f"Plugins initialized: {health['initialized']}/{health['total']} active"
                    + (f", {health['degraded']} degraded" if health["degraded"] else "")
                    + (f", {health['failed']} failed" if health["failed"] else "")
                )

                app.state.plugin_router = plugin_router
                set_plugin_router(plugin_router)
                # Background recovery: retries degraded/failed plugins with backoff
                # (e.g. Home Assistant on a server that's off at boot) and demotes
                # initialized plugins whose health_check starts failing.
                app.state.plugin_recovery_task = asyncio.create_task(
                    run_plugin_recovery(plugin_router)
                )
            else:
                application_logger.info("No plugins configured")
                app.state.plugin_router = None
                app.state.plugin_recovery_task = None

        except Exception as e:
            application_logger.error(
                f"Failed to initialize plugin system: {e}", exc_info=True
            )
            app.state.plugin_router = None
            app.state.plugin_recovery_task = None

    await asyncio.gather(
        _init_cron_scheduler(),
        _init_plugins(),
    )

    application_logger.info(
        f"Phase 4 (Cron/Plugins) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # Inbound vault edits (human edits in Obsidian, delivered by Syncthing) are
    # recorded into the memory audit ledger by a background listener. No-ops when
    # vault sync isn't configured.
    try:

        app.state.syncthing_audit_task = start_syncthing_audit_listener()
    except Exception as e:
        application_logger.warning(f"Syncthing memory-audit listener not started: {e}")
        app.state.syncthing_audit_task = None

    # Backstop reaper: one periodic loop that force-cleans stale clients (zombie
    # "connected" devices), orphaned audio streams the idle-timeout path missed, and
    # orphaned deferred RQ jobs whose dependency was deleted (never promotable).
    try:

        app.state.reaper_task = asyncio.create_task(run_reaper())
    except Exception as e:
        application_logger.warning(f"Reaper not started: {e}")
        app.state.reaper_task = None

    # Observability: drain the system-event ingest list (filled by RQ workers and the
    # catch-all log handler) into Mongo + SSE, and poll service health to record
    # crash-loop / down / recovered transitions.
    try:

        app.state.system_event_drain_task = asyncio.create_task(
            run_event_ingest_drain()
        )
        app.state.health_poller_task = asyncio.create_task(run_health_poller(app))
    except Exception as e:
        application_logger.warning(f"Observability tasks not started: {e}")
        app.state.system_event_drain_task = None
        app.state.health_poller_task = None

    # One-shot startup reconcile: recompute processing_status from facts once, so any
    # drift left before this version (or by a failure callback that itself died) is
    # healed at boot. Steady-state recovery is now event-driven — the post-conversation
    # chain uses Retry + Dependency(allow_failure=True) + an on_failure callback, so a
    # crashed/abandoned job recovers and surfaces a system event on its own without a
    # periodic poll. This boot sweep + the admin endpoint
    # (/api/admin/conversations/reconcile-status) are the remaining backstops. Run as a
    # background task so the (full-collection) scan doesn't block startup.
    try:

        app.state.status_reconciler_task = asyncio.create_task(
            reconcile_conversation_statuses()
        )
    except Exception as e:
        application_logger.warning(f"Startup status reconcile not started: {e}")
        app.state.status_reconciler_task = None

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

                await cleanup_client_state(client_id)
            except Exception as e:
                application_logger.error(f"Error cleaning up client {client_id}: {e}")

        # Stop the Syncthing memory-audit listener
        try:
            audit_task = getattr(app.state, "syncthing_audit_task", None)
            if audit_task is not None:
                audit_task.cancel()
                application_logger.info("Syncthing memory-audit listener stopped")
        except Exception as e:
            application_logger.error(f"Error stopping memory-audit listener: {e}")

        # Stop the backstop reaper
        try:
            reaper_task = getattr(app.state, "reaper_task", None)
            if reaper_task is not None:
                reaper_task.cancel()
                application_logger.info("Reaper stopped")
        except Exception as e:
            application_logger.error(f"Error stopping reaper: {e}")

        # Stop the plugin recovery loop
        try:
            recovery_task = getattr(app.state, "plugin_recovery_task", None)
            if recovery_task is not None:
                recovery_task.cancel()
                application_logger.info("Plugin recovery loop stopped")
        except Exception as e:
            application_logger.error(f"Error stopping plugin recovery loop: {e}")

        # Stop the observability tasks (event drain + health poller)
        for _attr, _label in (
            ("system_event_drain_task", "System-event drain"),
            ("health_poller_task", "Health poller"),
        ):
            try:
                _task = getattr(app.state, _attr, None)
                if _task is not None:
                    _task.cancel()
                    application_logger.info(f"{_label} stopped")
            except Exception as e:
                application_logger.error(f"Error stopping {_label}: {e}")

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

            await cleanup_plugin_router()
            application_logger.info("Plugins shut down")
        except Exception as e:
            application_logger.error(f"Error shutting down plugins: {e}")

        # Shutdown cron scheduler
        try:

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
    setup_middleware(
        app,
        disable_request_logging=os.getenv("DISABLE_REQUEST_LOGGING", "").lower()
        == "true",
    )

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

    logger.info(
        "FastAPI application created with all routers and middleware configured"
    )

    return app
