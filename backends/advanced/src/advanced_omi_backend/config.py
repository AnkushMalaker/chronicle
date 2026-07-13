"""
Configuration management for Chronicle backend.

Uses OmegaConf for unified YAML configuration with environment variable interpolation.
Secrets are stored in .env files, all other config in config/config.yml.
"""

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import (
    get_backend_config,
    get_config_dir,
    load_config,
)
from advanced_omi_backend.config_loader import reload_config as reload_omegaconf_config
from advanced_omi_backend.config_loader import save_config_section

logger = logging.getLogger(__name__)

# Data directory paths
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CHUNK_DIR = Path("./audio_chunks")  # Mounted to ./data/audio_chunks by Docker

# Liveness: close a WebSocket that sends nothing for this many seconds. A streaming
# device emits chunks every ~0.25s, so this only ever trips on a genuinely dead peer
# (including a relay holding the socket open after its device vanished). Doubles as the
# freshness window used to decide whether a client counts as "connected" on the Network
# page. Default 5 min — conservative enough that an idle-but-alive client (armed device,
# app holding a control socket) isn't churned, while still bounding a zombie to minutes
# instead of forever. A falsely-reaped client simply auto-reconnects. Lower it if you
# want tighter detection and your clients stream or ping continuously.
WS_IDLE_TIMEOUT_SECS = float(os.getenv("WS_IDLE_TIMEOUT_SECS", "300"))


# ============================================================================
# Configuration Functions (OmegaConf-based)
# ============================================================================


def get_config_yml_path() -> Path:
    """
    Get path to config.yml file.

    Returns:
        Path to config.yml
    """
    return get_config_dir() / "config.yml"


def get_config(force_reload: bool = False) -> dict:
    """
    Get merged configuration using OmegaConf.

    Wrapper around load_config() from config_loader for backward compatibility.

    Args:
        force_reload: If True, reload from disk even if cached

    Returns:
        Merged configuration dictionary with all settings
    """
    cfg = load_config(force_reload=force_reload)
    return OmegaConf.to_container(cfg, resolve=True)


def reload_config():
    """Reload configuration from disk (invalidate cache)."""
    return reload_omegaconf_config()


# ============================================================================
# Diarization Settings (OmegaConf-based)
# ============================================================================


def get_diarization_settings() -> dict:
    """
    Get diarization settings using OmegaConf.

    Returns:
        Dict with diarization configuration (resolved from YAML + env vars)
    """
    cfg = get_backend_config("diarization")
    return OmegaConf.to_container(cfg, resolve=True)


def save_diarization_settings(settings: dict) -> bool:
    """
    Save diarization settings to config.yml using OmegaConf.

    Args:
        settings: Dict with diarization settings to save

    Returns:
        True if saved successfully, False otherwise
    """
    return save_config_section("backend.diarization", settings)


# ============================================================================
# Cleanup Settings (OmegaConf-based)
# ============================================================================


@dataclass
class CleanupSettings:
    """Cleanup configuration for soft-deleted conversations."""

    auto_cleanup_enabled: bool = False
    retention_days: int = 30


def get_cleanup_settings() -> dict:
    """
    Get cleanup settings using OmegaConf.

    Returns:
        Dict with auto_cleanup_enabled and retention_days
    """
    cfg = get_backend_config("cleanup")
    return OmegaConf.to_container(cfg, resolve=True)


def save_cleanup_settings(settings: CleanupSettings) -> bool:
    """
    Save cleanup settings to config.yml using OmegaConf.

    Args:
        settings: CleanupSettings dataclass instance

    Returns:
        True if saved successfully, False otherwise
    """
    return save_config_section("backend.cleanup", asdict(settings))


# ============================================================================
# Speech Detection Settings (OmegaConf-based)
# ============================================================================


def get_speech_detection_settings() -> dict:
    """
    Get speech detection settings using OmegaConf.

    Returns:
        Dict with min_words, min_confidence, min_duration
    """
    cfg = get_backend_config("speech_detection")
    return OmegaConf.to_container(cfg, resolve=True)


# ============================================================================
# Conversation Stop Settings (OmegaConf-based)
# ============================================================================


def get_conversation_stop_settings() -> dict:
    """
    Get conversation stop settings using OmegaConf.

    Returns:
        Dict with speech_inactivity_threshold
    """
    cfg = get_backend_config("conversation_stop")
    settings = OmegaConf.to_container(cfg, resolve=True)

    # Add min_word_confidence from speech_detection for backward compatibility
    speech_cfg = get_backend_config("speech_detection")
    settings["min_word_confidence"] = OmegaConf.to_container(
        speech_cfg, resolve=True
    ).get("min_confidence", 0.7)

    return settings


# ============================================================================
# Audio Storage Settings (OmegaConf-based)
# ============================================================================


def get_audio_storage_settings() -> dict:
    """
    Get audio storage settings using OmegaConf.

    Returns:
        Dict with audio_base_path, audio_chunks_path
    """
    cfg = get_backend_config("audio_storage")
    return OmegaConf.to_container(cfg, resolve=True)


# ============================================================================
# Streaming Fallback Timeout (OmegaConf-based)
# ============================================================================


def get_streaming_fallback_timeout() -> int:
    """
    Get timeout for the streaming fallback check in seconds.

    This controls how long the fallback check job waits for batch
    transcription to complete before giving up. Not an RQ job timeout.

    Returns:
        Fallback timeout in seconds (default 120 = 2 minutes)
    """
    cfg = get_backend_config("transcription")
    settings = OmegaConf.to_container(cfg, resolve=True) if cfg else {}
    # Try new key first, fall back to old key for compat
    timeout = settings.get("streaming_fallback_timeout_seconds")
    if timeout is None:
        timeout = settings.get("job_timeout_seconds", 120)
    return int(timeout)


def get_live_segmentation() -> str:
    """
    Get the live-transcription mode (top-level ``defaults.live_segmentation``).

    - ``"streaming_stt"`` (default): a real streaming ASR produces live transcripts.
    - ``"windowed_batch"``: pseudo-streaming via fixed-duration batch windows.
    - ``"off"``: no live transcription; the final transcript is produced by batch
      transcription only when the session ends.

    Returns:
        One of "streaming_stt", "windowed_batch", "off".
    """
    cfg = load_config()
    defaults_settings = (
        OmegaConf.to_container(cfg.get("defaults", {}), resolve=True) if cfg else {}
    ) or {}
    return defaults_settings.get("live_segmentation", "streaming_stt")


# ============================================================================
# Wake-Word Command Source (OmegaConf-based)
# ============================================================================

# Valid values for backend.wakeword.command_source.
WAKEWORD_COMMAND_SOURCES = ("batch", "streaming", "batch_then_streaming")


def get_wakeword_command_source() -> str:
    """How the acoustic wake-word command text is obtained.

    The standalone wakeword-service captures the post-wake-word turn and the
    dispatcher turns it into a command string. This controls how:

    - ``"batch"``: batch-transcribe the captured command audio via the configured
      batch STT provider. Highest quality, but the command silently fails if that
      service is down (the streaming live transcript is unaffected, so it looks
      like only commands break).
    - ``"streaming"``: trust the live streaming transcript for the capture window
      and skip batch ASR entirely. Useful when the streaming provider is as good
      as (or better than) the batch one, or to avoid running a second ASR.
    - ``"batch_then_streaming"`` (default): batch ASR, but fall back to the
      streaming transcript — with a WARNING log and a degraded ``asr_status`` —
      when batch is unreachable or returns an empty command.

    Lives at ``backend.wakeword.command_source`` in config.yml.
    """
    cfg = get_backend_config("wakeword")
    settings = OmegaConf.to_container(cfg, resolve=True) if cfg else {}
    source = (settings or {}).get("command_source", "batch_then_streaming")
    if source not in WAKEWORD_COMMAND_SOURCES:
        logger.warning(
            "Invalid backend.wakeword.command_source=%r; falling back to "
            "'batch_then_streaming'",
            source,
        )
        return "batch_then_streaming"
    return source


# ============================================================================
# Miscellaneous Settings (OmegaConf-based)
# ============================================================================


def get_misc_settings() -> dict:
    """
    Get miscellaneous configuration settings using OmegaConf.

    Returns:
        Dict with miscellaneous settings (persistence, timeouts, segmentation mode)
    """
    # Get audio settings for always_persist_enabled
    audio_cfg = get_backend_config("audio")
    audio_settings = (
        OmegaConf.to_container(audio_cfg, resolve=True) if audio_cfg else {}
    )

    # Get transcription settings for timeouts and batch re-transcription
    transcription_cfg = get_backend_config("transcription")
    transcription_settings = (
        OmegaConf.to_container(transcription_cfg, resolve=True)
        if transcription_cfg
        else {}
    )

    # Get speaker recognition settings for per_segment_speaker_id
    speaker_cfg = get_backend_config("speaker_recognition")
    speaker_settings = (
        OmegaConf.to_container(speaker_cfg, resolve=True) if speaker_cfg else {}
    )

    # Live-transcription mode lives in the top-level `defaults` block, not under
    # `backend`. "windowed_batch" is the pseudo-streaming-via-batch preview;
    # "off" disables the live preview entirely (final transcript still produced
    # by batch transcription when the conversation ends).
    cfg = load_config()
    defaults_settings = (
        OmegaConf.to_container(cfg.get("defaults", {}), resolve=True) if cfg else {}
    ) or {}

    return {
        "always_persist_enabled": audio_settings.get("always_persist_enabled", False),
        "per_segment_speaker_id": speaker_settings.get("per_segment_speaker_id", False),
        "streaming_fallback_timeout_seconds": int(
            transcription_settings.get(
                "streaming_fallback_timeout_seconds",
                transcription_settings.get("job_timeout_seconds", 120),
            )
        ),
        "always_batch_retranscribe": transcription_settings.get(
            "always_batch_retranscribe", False
        ),
        "live_segmentation": defaults_settings.get(
            "live_segmentation", "streaming_stt"
        ),
    }


def save_misc_settings(settings: dict) -> bool:
    """
    Save miscellaneous settings to config.yml using OmegaConf.

    Args:
        settings: Dict with miscellaneous settings to save

    Returns:
        True if saved successfully, False otherwise
    """
    success = True

    # Save audio settings if always_persist_enabled is provided
    if "always_persist_enabled" in settings:
        audio_settings = {"always_persist_enabled": settings["always_persist_enabled"]}
        if not save_config_section("backend.audio", audio_settings):
            success = False

    # Save speaker recognition settings if per_segment_speaker_id is provided
    if "per_segment_speaker_id" in settings:
        speaker_settings = {
            "per_segment_speaker_id": settings["per_segment_speaker_id"]
        }
        if not save_config_section("backend.speaker_recognition", speaker_settings):
            success = False

    # Save streaming fallback timeout if provided
    if "streaming_fallback_timeout_seconds" in settings:
        timeout_settings = {
            "streaming_fallback_timeout_seconds": settings[
                "streaming_fallback_timeout_seconds"
            ]
        }
        if not save_config_section("backend.transcription", timeout_settings):
            success = False

    # Save always_batch_retranscribe if provided
    if "always_batch_retranscribe" in settings:
        batch_settings = {
            "always_batch_retranscribe": settings["always_batch_retranscribe"]
        }
        if not save_config_section("backend.transcription", batch_settings):
            success = False

    # Save live_segmentation if provided (top-level `defaults` block). Note: this
    # selects which live worker the orchestrator runs, so it only takes effect
    # after the workers container is restarted.
    if "live_segmentation" in settings:
        if not save_config_section(
            "defaults", {"live_segmentation": settings["live_segmentation"]}
        ):
            success = False

    return success
