"""
Worker Registry

Builds the complete list of worker definitions with conditional logic.
Reuses model_registry.py for config.yml parsing.
"""

import logging
import os
from typing import List

from .config import WorkerDefinition, WorkerType

logger = logging.getLogger(__name__)


def _get_live_segmentation() -> str:
    """Read the live-segmentation mode from config.yml (defaults.live_segmentation).

    "streaming_stt" (default): the streaming-stt worker produces live transcripts.
    "windowed_batch": the windowed-batch worker produces transcripts from the batch
        STT provider in fixed-duration windows (for setups with no streaming ASR).

    Exactly one of the two transcript-producing workers runs, gated on this switch.
    """
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if registry and registry.defaults:
            return registry.defaults.get("live_segmentation", "streaming_stt")
    except Exception as e:
        logger.warning(f"Failed to read live_segmentation from config.yml: {e}")

    return "streaming_stt"


def has_streaming_stt_configured() -> bool:
    """
    Check if the streaming STT worker should run.

    Returns:
        True if live_segmentation is "streaming_stt" and defaults.stt_stream is configured.

    Note: Batch STT is handled by RQ workers in transcription_jobs.py,
          no separate worker needed.
    """
    if _get_live_segmentation() != "streaming_stt":
        return False
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if registry and registry.defaults:
            stt_stream_model = registry.get_default("stt_stream")
            return stt_stream_model is not None
    except Exception as e:
        logger.warning(f"Failed to read streaming STT config from config.yml: {e}")

    return False


def has_windowed_batch_configured() -> bool:
    """
    Check if the windowed-batch transcription worker should run.

    Returns:
        True if live_segmentation is "windowed_batch" and a batch STT provider
        (defaults.stt) is configured.
    """
    if _get_live_segmentation() != "windowed_batch":
        return False
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if registry and registry.defaults:
            return registry.get_default("stt") is not None
    except Exception as e:
        logger.warning(f"Failed to read batch STT config from config.yml: {e}")

    return False


def has_wakeword_dispatch_enabled() -> bool:
    """
    Check if the wake-word dispatch worker should run.

    The dispatcher bridges the standalone wakeword-service's ``wakeword:detections``
    Redis stream to the plugin router (``wake_word.detected`` event). It must run
    independently of the live-transcription mode — otherwise switching to
    ``windowed_batch`` (no streaming-stt worker) silently kills the acoustic
    wake-word → plugin path.

    Returns:
        True if any enabled plugin subscribes to the ``wake_word.detected`` event.
    """
    try:
        import yaml

        from advanced_omi_backend.config_loader import get_plugins_yml_path

        plugins_yml = get_plugins_yml_path()
        if not plugins_yml.exists():
            return False
        with open(plugins_yml, "r") as f:
            plugins_config = yaml.safe_load(f) or {}
        for _plugin_id, orchestration in (plugins_config.get("plugins") or {}).items():
            if not isinstance(orchestration, dict):
                continue
            if not orchestration.get("enabled", False):
                continue
            if "wake_word.detected" in (orchestration.get("events") or []):
                return True
    except Exception as e:
        logger.warning(
            f"Failed to read wake-word dispatch config from plugins.yml: {e}"
        )

    return False


def build_worker_definitions() -> List[WorkerDefinition]:
    """
    Build the complete list of worker definitions.

    Returns:
        List of WorkerDefinition objects, including conditional workers
    """
    workers = []

    # 6x RQ Workers - Multi-queue workers (transcription, memory, default)
    for i in range(1, 7):
        workers.append(
            WorkerDefinition(
                name=f"rq-worker-{i}",
                command=[
                    "python",
                    "-m",
                    "advanced_omi_backend.workers.rq_worker_entry",
                    "transcription",
                    "memory",
                    "default",
                ],
                worker_type=WorkerType.RQ_WORKER,
                queues=["transcription", "memory", "default"],
                restart_on_failure=True,
            )
        )

    # Audio Persistence Workers - Single-queue workers (audio queue)
    # Multiple workers allow concurrent audio persistence for multiple sessions
    for i in range(1, 4):  # 3 audio workers
        workers.append(
            WorkerDefinition(
                name=f"audio-persistence-{i}",
                command=[
                    "python",
                    "-m",
                    "advanced_omi_backend.workers.rq_worker_entry",
                    "audio",
                ],
                worker_type=WorkerType.RQ_WORKER,
                queues=["audio"],
                restart_on_failure=True,
            )
        )

    # Streaming STT Worker - Conditional (if streaming STT is configured in config.yml)
    # This worker uses the registry-driven streaming provider (RegistryStreamingTranscriptionProvider)
    # Batch transcription happens via RQ jobs in transcription_jobs.py (already uses registry provider)
    workers.append(
        WorkerDefinition(
            name="streaming-stt",
            command=[
                "python",
                "-m",
                "advanced_omi_backend.workers.audio_stream_worker",
            ],
            worker_type=WorkerType.STREAM_CONSUMER,
            enabled_check=has_streaming_stt_configured,
            restart_on_failure=True,
        )
    )

    # Windowed Batch Worker - Conditional (live_segmentation == "windowed_batch").
    # Mutually exclusive with streaming-stt via the live_segmentation switch. Transcribes
    # fixed-duration windows with the batch STT provider so continuous/static sources are
    # transcribed incrementally instead of only on disconnect (no streaming ASR needed).
    workers.append(
        WorkerDefinition(
            name="windowed-batch",
            command=[
                "python",
                "-m",
                "advanced_omi_backend.workers.windowed_batch_worker",
            ],
            worker_type=WorkerType.STREAM_CONSUMER,
            enabled_check=has_windowed_batch_configured,
            restart_on_failure=True,
        )
    )

    # Wake-word Dispatch Worker - Conditional (any enabled plugin subscribes to
    # wake_word.detected). Runs independently of the live-transcription mode so the
    # acoustic wake-word → plugin path keeps working under windowed_batch (where the
    # streaming-stt worker, which used to host the dispatcher, does not run).
    workers.append(
        WorkerDefinition(
            name="wakeword-dispatch",
            command=[
                "python",
                "-m",
                "advanced_omi_backend.workers.wakeword_dispatch_worker",
            ],
            worker_type=WorkerType.STREAM_CONSUMER,
            enabled_check=has_wakeword_dispatch_enabled,
            restart_on_failure=True,
        )
    )

    # Log worker configuration
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if registry:
            stt_stream = registry.get_default("stt_stream")
            stt_batch = registry.get_default("stt")
            if stt_stream:
                logger.info(
                    f"Streaming STT configured: {stt_stream.name} ({stt_stream.model_provider})"
                )
            if stt_batch:
                logger.info(
                    f"Batch STT configured: {stt_batch.name} ({stt_batch.model_provider}) - handled by RQ workers"
                )
    except Exception as e:
        logger.warning(f"Failed to log STT configuration: {e}")

    enabled_workers = [w for w in workers if w.is_enabled()]
    disabled_workers = [w for w in workers if not w.is_enabled()]

    logger.info(f"Total workers configured: {len(workers)}")
    logger.info(f"Enabled workers: {len(enabled_workers)}")
    logger.info(f"Enabled worker names: {', '.join([w.name for w in enabled_workers])}")

    if disabled_workers:
        logger.info(
            f"Disabled workers: {', '.join([w.name for w in disabled_workers])}"
        )

    return enabled_workers
