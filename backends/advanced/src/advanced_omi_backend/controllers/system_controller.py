"""
System controller for handling system-related business logic.
"""

import asyncio
import inspect
import logging
import os
import re
import shutil
import signal
import time
import warnings
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from ruamel.yaml import YAML

from advanced_omi_backend.config import (
    get_diarization_settings as load_diarization_settings,
)
from advanced_omi_backend.config import get_misc_settings as load_misc_settings
from advanced_omi_backend.config import save_diarization_settings, save_misc_settings
from advanced_omi_backend.config_loader import get_plugins_yml_path, save_config_section
from advanced_omi_backend.model_registry import (
    _find_config_path,
    get_models_registry,
    load_models_config,
)
from advanced_omi_backend.models.user import User

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

_yaml = YAML()
_yaml.preserve_quotes = True


async def get_config_diagnostics():
    """
    Get comprehensive configuration diagnostics.

    Returns warnings, errors, and status for all configuration components.
    """
    diagnostics = {
        "timestamp": datetime.now(UTC).isoformat(),
        "overall_status": "healthy",
        "issues": [],
        "warnings": [],
        "info": [],
        "components": {},
    }

    # Test OmegaConf configuration loading
    try:
        from advanced_omi_backend.config_loader import load_config

        # Capture warnings during config load
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = load_config(force_reload=True)

            # Check for OmegaConf warnings
            for warning in w:
                warning_msg = str(warning.message)
                if "some elements are missing" in warning_msg.lower():
                    # Extract the variable name from warning
                    if "variable '" in warning_msg.lower():
                        var_name = warning_msg.split("'")[1]
                        diagnostics["warnings"].append(
                            {
                                "component": "OmegaConf",
                                "severity": "warning",
                                "message": f"Environment variable '{var_name}' not set (using empty default)",
                                "resolution": f"Set {var_name} in .env file if needed",
                            }
                        )

        diagnostics["components"]["omegaconf"] = {
            "status": "healthy",
            "message": "Configuration loaded successfully",
        }
    except Exception as e:
        diagnostics["overall_status"] = "unhealthy"
        diagnostics["issues"].append(
            {
                "component": "OmegaConf",
                "severity": "error",
                "message": f"Failed to load configuration: {str(e)}",
                "resolution": "Check config/defaults.yml and config/config.yml syntax",
            }
        )
        diagnostics["components"]["omegaconf"] = {"status": "unhealthy", "message": str(e)}

    # Test model registry
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry = get_models_registry()

            # Capture model loading warnings
            for warning in w:
                warning_msg = str(warning.message)
                diagnostics["warnings"].append(
                    {
                        "component": "Model Registry",
                        "severity": "warning",
                        "message": warning_msg,
                        "resolution": "Check model definitions in config/defaults.yml",
                    }
                )

        if registry:
            diagnostics["components"]["model_registry"] = {
                "status": "healthy",
                "message": f"Loaded {len(registry.models)} models",
                "details": {
                    "total_models": len(registry.models),
                    "defaults": dict(registry.defaults) if registry.defaults else {},
                },
            }

            # Check critical models
            stt = registry.get_default("stt")
            stt_stream = registry.get_default("stt_stream")
            llm = registry.get_default("llm")

            # STT check
            if stt:
                if stt.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "STT (Batch)",
                            "message": f"Configured: {stt.name} ({stt.model_provider}) - API key present",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "STT (Batch)",
                            "severity": "warning",
                            "message": f"{stt.name} ({stt.model_provider}) - No API key configured",
                            "resolution": "Transcription can fail without API key",
                        }
                    )
            else:
                diagnostics["issues"].append(
                    {
                        "component": "STT (Batch)",
                        "severity": "error",
                        "message": "No batch STT model configured",
                        "resolution": "Set defaults.stt in config.yml",
                    }
                )
                diagnostics["overall_status"] = "partial"

            # Streaming STT check
            if stt_stream:
                if stt_stream.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "STT (Streaming)",
                            "message": f"Configured: {stt_stream.name} ({stt_stream.model_provider}) - API key present",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "STT (Streaming)",
                            "severity": "warning",
                            "message": f"{stt_stream.name} ({stt_stream.model_provider}) - No API key configured",
                            "resolution": "Real-time transcription can fail without API key",
                        }
                    )
            else:
                diagnostics["warnings"].append(
                    {
                        "component": "STT (Streaming)",
                        "severity": "warning",
                        "message": "No streaming STT model configured - streaming worker disabled",
                        "resolution": "Set defaults.stt_stream in config.yml for WebSocket transcription",
                    }
                )

            # LLM check
            if llm:
                if llm.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "LLM",
                            "message": f"Configured: {llm.name} ({llm.model_provider}) - API key present",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "LLM",
                            "severity": "warning",
                            "message": f"{llm.name} ({llm.model_provider}) - No API key configured",
                            "resolution": "Memory extraction can fail without API key",
                        }
                    )

        else:
            diagnostics["overall_status"] = "unhealthy"
            diagnostics["issues"].append(
                {
                    "component": "Model Registry",
                    "severity": "error",
                    "message": "Failed to load model registry",
                    "resolution": "Check config/defaults.yml for syntax errors",
                }
            )
            diagnostics["components"]["model_registry"] = {
                "status": "unhealthy",
                "message": "Registry failed to load",
            }
    except Exception as e:
        diagnostics["overall_status"] = "partial"
        diagnostics["issues"].append(
            {
                "component": "Model Registry",
                "severity": "error",
                "message": f"Error loading registry: {str(e)}",
                "resolution": "Check logs for detailed error information",
            }
        )
        diagnostics["components"]["model_registry"] = {"status": "unhealthy", "message": str(e)}

    # Check environment variables (only warn about keys relevant to configured providers)
    env_checks = [
        ("AUTH_SECRET_KEY", "Required for authentication"),
        ("ADMIN_EMAIL", "Required for admin user login"),
        ("ADMIN_PASSWORD", "Required for admin user login"),
    ]

    if registry:
        # Add LLM API key check based on active provider
        llm_model = registry.get_default("llm")
        if llm_model and llm_model.model_provider == "openai":
            env_checks.append(("OPENAI_API_KEY", "Required for OpenAI LLM and embeddings"))
        elif llm_model and llm_model.model_provider == "groq":
            env_checks.append(("GROQ_API_KEY", "Required for Groq LLM"))

        # Add transcription API key check based on active STT provider
        stt_model = registry.get_default("stt")
        if stt_model:
            provider = stt_model.model_provider
            if provider == "deepgram":
                env_checks.append(("DEEPGRAM_API_KEY", "Required for Deepgram transcription"))
            elif provider == "smallest":
                env_checks.append(
                    ("SMALLEST_API_KEY", "Required for Smallest.ai Pulse transcription")
                )

    for env_var, description in env_checks:
        value = os.getenv(env_var)
        if not value or value == "":
            diagnostics["warnings"].append(
                {
                    "component": "Environment Variables",
                    "severity": "warning",
                    "message": f"{env_var} not set - {description}",
                    "resolution": f"Set {env_var} in .env file",
                }
            )

    return diagnostics


async def get_current_metrics():
    """Get current system metrics."""
    try:
        # Get memory provider configuration
        memory_provider = (await get_memory_provider())["current_provider"]

        # Get basic system metrics
        metrics = {
            "timestamp": int(time.time()),
            "memory_provider": memory_provider,
            "memory_provider_supports_threshold": memory_provider == "chronicle",
        }

        return metrics

    except Exception as e:
        audio_logger.exception("Error fetching metrics")
        raise e


async def get_auth_config():
    """Get authentication configuration for frontend."""
    return {
        "auth_method": "email",
        "registration_enabled": False,  # Only admin can create users
        "features": {
            "email_login": True,
            "user_id_login": False,  # Deprecated
            "registration": False,
        },
    }


async def get_observability_config():
    """Get observability configuration for frontend (Langfuse deep-links).

    Returns non-secret data only (enabled status and browser URL).
    """
    from advanced_omi_backend.observability.otel_setup import is_langfuse_enabled

    enabled = is_langfuse_enabled()
    session_base_url = None

    if enabled:
        from advanced_omi_backend.config_loader import load_config

        cfg = load_config()
        public_url = cfg.get("observability", {}).get("langfuse", {}).get("public_url", "")
        if public_url:
            # Strip trailing slash and build session URL
            session_base_url = f"{public_url.rstrip('/')}/project/chronicle/sessions"

    return {
        "langfuse": {
            "enabled": enabled,
            "session_base_url": session_base_url,
        }
    }


# Audio file processing functions moved to audio_controller.py


# Configuration functions moved to config.py to avoid circular imports


async def get_diarization_settings():
    """Get current diarization settings."""
    try:
        # Get settings using OmegaConf
        settings = load_diarization_settings()
        return {"settings": settings, "status": "success"}
    except Exception as e:
        logger.exception("Error getting diarization settings")
        raise e


async def save_diarization_settings_controller(settings: dict):
    """Save diarization settings."""
    try:
        # Validate settings
        valid_keys = {
            "diarization_source",
            "similarity_threshold",
            "min_duration",
            "collar",
            "min_duration_off",
            "min_speakers",
            "max_speakers",
        }

        # Filter to only valid keys (allow round-trip GET→POST)
        filtered_settings = {}
        for key, value in settings.items():
            if key not in valid_keys:
                continue  # Skip unknown keys instead of rejecting

            # Type validation for known keys only
            if key in ["min_speakers", "max_speakers"]:
                if not isinstance(value, int) or value < 1 or value > 20:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid value for {key}: must be integer 1-20"
                    )
            elif key == "diarization_source":
                if not isinstance(value, str) or value not in ["pyannote", "deepgram"]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be 'pyannote' or 'deepgram'",
                    )
            else:
                if not isinstance(value, (int, float)) or value < 0:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid value for {key}: must be positive number"
                    )

            filtered_settings[key] = value

        # Reject if NO valid keys provided (completely invalid request)
        if not filtered_settings:
            raise HTTPException(status_code=400, detail="No valid diarization settings provided")

        # Get current settings and merge with new values
        current_settings = load_diarization_settings()
        current_settings.update(filtered_settings)

        # Save using OmegaConf
        if save_diarization_settings(current_settings):
            logger.info(f"Updated and saved diarization settings: {filtered_settings}")

            return {
                "message": "Diarization settings saved successfully",
                "settings": current_settings,
                "status": "success",
            }
        else:
            logger.warning("Settings save failed")
            return {
                "message": "Settings save failed",
                "settings": current_settings,
                "status": "error",
            }

    except Exception as e:
        logger.exception("Error saving diarization settings")
        raise e


async def get_misc_settings():
    """Get current miscellaneous settings."""
    try:
        # Get settings using OmegaConf
        settings = load_misc_settings()
        return {"settings": settings, "status": "success"}
    except Exception as e:
        logger.exception("Error getting misc settings")
        raise e


async def save_misc_settings_controller(settings: dict):
    """Save miscellaneous settings."""
    try:
        # Validate settings
        boolean_keys = {
            "always_persist_enabled",
            "use_provider_segments",
            "per_segment_speaker_id",
            "always_batch_retranscribe",
        }
        integer_keys = {"transcription_job_timeout_seconds"}
        valid_keys = boolean_keys | integer_keys

        # Filter to only valid keys
        filtered_settings = {}
        for key, value in settings.items():
            if key not in valid_keys:
                continue  # Skip unknown keys

            # Type validation
            if key in boolean_keys:
                if not isinstance(value, bool):
                    raise HTTPException(
                        status_code=400, detail=f"Invalid value for {key}: must be boolean"
                    )
            elif key == "transcription_job_timeout_seconds":
                if not isinstance(value, int) or value < 60 or value > 7200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be integer between 60 and 7200",
                    )

            filtered_settings[key] = value

        # Reject if NO valid keys provided
        if not filtered_settings:
            raise HTTPException(status_code=400, detail="No valid misc settings provided")

        # Save using OmegaConf
        if save_misc_settings(filtered_settings):
            # Get updated settings
            updated_settings = load_misc_settings()
            logger.info(f"Updated and saved misc settings: {filtered_settings}")

            return {
                "message": "Miscellaneous settings saved successfully",
                "settings": updated_settings,
                "status": "success",
            }
        else:
            logger.warning("Settings save failed")
            return {
                "message": "Settings save failed",
                "settings": load_misc_settings(),
                "status": "error",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error saving misc settings")
        raise e


async def get_cleanup_settings_controller(user: User) -> dict:
    """
    Get current cleanup settings (admin only).

    Args:
        user: Authenticated admin user

    Returns:
        Dict with cleanup settings
    """
    from advanced_omi_backend.config import get_cleanup_settings

    return get_cleanup_settings()


async def save_cleanup_settings_controller(
    auto_cleanup_enabled: bool, retention_days: int, user: User
) -> dict:
    """
    Save cleanup settings (admin only).

    Args:
        auto_cleanup_enabled: Enable/disable automatic cleanup
        retention_days: Number of days to retain soft-deleted conversations
        user: Authenticated admin user

    Returns:
        Updated cleanup settings

    Raises:
        ValueError: If validation fails
    """
    from advanced_omi_backend.config import CleanupSettings, save_cleanup_settings

    # Validation
    if not isinstance(auto_cleanup_enabled, bool):
        raise ValueError("auto_cleanup_enabled must be a boolean")

    if not isinstance(retention_days, int):
        raise ValueError("retention_days must be an integer")

    if retention_days < 1 or retention_days > 365:
        raise ValueError("retention_days must be between 1 and 365")

    # Create settings object
    settings = CleanupSettings(
        auto_cleanup_enabled=auto_cleanup_enabled, retention_days=retention_days
    )

    # Save using OmegaConf
    save_cleanup_settings(settings)

    logger.info(
        f"Admin {user.email} updated cleanup settings: auto_cleanup={auto_cleanup_enabled}, retention={retention_days}d"
    )

    return {
        "auto_cleanup_enabled": settings.auto_cleanup_enabled,
        "retention_days": settings.retention_days,
        "message": "Cleanup settings saved successfully",
    }


async def get_speaker_configuration(user: User):
    """Get current user's primary speakers configuration."""
    try:
        return {
            "primary_speakers": user.primary_speakers,
            "user_id": user.user_id,
            "status": "success",
        }
    except Exception as e:
        logger.exception(f"Error getting speaker configuration for user {user.user_id}")
        raise e


async def update_speaker_configuration(user: User, primary_speakers: list[dict]):
    """Update current user's primary speakers configuration."""
    try:
        # Validate speaker data format
        for speaker in primary_speakers:
            if not isinstance(speaker, dict):
                raise ValueError("Each speaker must be a dictionary")

            required_fields = ["speaker_id", "name", "user_id"]
            for field in required_fields:
                if field not in speaker:
                    raise ValueError(f"Missing required field: {field}")

        # Enforce server-side user_id and add timestamp to each speaker
        for speaker in primary_speakers:
            speaker["user_id"] = user.user_id  # Override client-supplied user_id
            speaker["selected_at"] = datetime.now(UTC).isoformat()

        # Update user model
        user.primary_speakers = primary_speakers
        await user.save()

        logger.info(
            f"Updated primary speakers configuration for user {user.user_id}: {len(primary_speakers)} speakers"
        )

        return {
            "message": "Primary speakers configuration updated successfully",
            "primary_speakers": primary_speakers,
            "count": len(primary_speakers),
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error updating speaker configuration for user {user.user_id}")
        raise e


async def get_enrolled_speakers(user: User):
    """Get enrolled speakers from speaker recognition service."""
    try:
        from advanced_omi_backend.speaker_recognition_client import (
            SpeakerRecognitionClient,
        )

        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()

        if not speaker_client.enabled:
            return {
                "speakers": [],
                "service_available": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "success",
            }

        # Get enrolled speakers - using hardcoded user_id=1 for now (as noted in speaker_recognition_client.py)
        speakers = await speaker_client.get_enrolled_speakers(user_id="1")

        return {
            "speakers": speakers.get("speakers", []) if speakers else [],
            "service_available": True,
            "message": "Successfully retrieved enrolled speakers",
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error getting enrolled speakers for user {user.user_id}")
        raise e


async def get_speaker_service_status():
    """Check speaker recognition service health status."""
    try:
        from advanced_omi_backend.speaker_recognition_client import (
            SpeakerRecognitionClient,
        )

        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()

        if not speaker_client.enabled:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "disabled",
            }

        # Perform health check
        health_result = await speaker_client.health_check()

        if health_result:
            return {
                "service_available": True,
                "healthy": True,
                "message": "Speaker recognition service is healthy",
                "service_url": speaker_client.service_url,
                "status": "healthy",
            }
        else:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not responding",
                "service_url": speaker_client.service_url,
                "status": "unhealthy",
            }

    except Exception as e:
        logger.exception("Error checking speaker service status")
        raise e


# Memory Configuration Management Functions


async def get_memory_config_raw():
    """Get current memory configuration (memory section of config.yml) as YAML."""
    try:
        cfg_path = _find_config_path()
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"Config file not found: {cfg_path}")

        with open(cfg_path, "r") as f:
            data = _yaml.load(f) or {}
        memory_section = data.get("memory", {})
        stream = StringIO()
        _yaml.dump(dict(memory_section) if memory_section else {}, stream)
        config_yaml = stream.getvalue()

        return {
            "config_yaml": config_yaml,
            "config_path": str(cfg_path),
            "section": "memory",
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error reading memory config")
        raise e


async def update_memory_config_raw(config_yaml: str):
    """Update memory configuration in config.yml and hot reload registry."""
    try:
        # Validate YAML
        try:
            new_mem = _yaml.load(config_yaml) or {}
        except Exception as e:
            raise ValueError(f"Invalid YAML syntax: {str(e)}")

        cfg_path = _find_config_path()
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"Config file not found: {cfg_path}")

        # Backup
        backup_path = f"{cfg_path}.bak"
        shutil.copy2(cfg_path, backup_path)

        # Update memory section and write file
        with open(cfg_path, "r") as f:
            data = _yaml.load(f) or {}
        data["memory"] = new_mem
        with open(cfg_path, "w") as f:
            _yaml.dump(data, f)

        # Reload registry
        load_models_config(force_reload=True)

        return {
            "message": "Memory configuration updated and reloaded successfully",
            "config_path": str(cfg_path),
            "backup_created": os.path.exists(backup_path),
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error updating memory config")
        raise e


async def validate_memory_config(config_yaml: str):
    """Validate memory configuration YAML syntax (memory section)."""
    try:
        try:
            parsed = _yaml.load(config_yaml)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML syntax: {str(e)}")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="Configuration must be a YAML object")
        # Minimal checks
        # provider optional; timeout_seconds optional; extraction enabled/prompt optional
        return {"message": "Configuration is valid", "status": "success"}
    except HTTPException:
        # Re-raise HTTPExceptions without wrapping
        raise
    except Exception as e:
        logger.exception("Error validating memory config")
        raise HTTPException(status_code=500, detail=f"Error validating memory config: {str(e)}")


async def reload_memory_config():
    """Reload config.yml (registry)."""
    try:
        cfg_path = _find_config_path()
        load_models_config(force_reload=True)
        return {
            "message": "Configuration reloaded",
            "config_path": str(cfg_path),
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error reloading config")
        raise e


async def delete_all_user_memories(user: User):
    """Delete all memories for the current user."""
    try:
        from advanced_omi_backend.services.memory import get_memory_service

        memory_service = get_memory_service()

        # Delete all memories for the user
        deleted_count = await memory_service.delete_all_user_memories(user.user_id)

        logger.info(f"Deleted {deleted_count} memories for user {user.user_id}")

        return {
            "message": f"Successfully deleted {deleted_count} memories",
            "deleted_count": deleted_count,
            "user_id": user.user_id,
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error deleting all memories for user {user.user_id}")
        raise e


# Memory Provider Configuration Functions


async def get_memory_provider():
    """Get current memory provider configuration."""
    try:
        current_provider = os.getenv("MEMORY_PROVIDER", "chronicle").lower()
        # Map legacy provider names to current names
        if current_provider in ("friend-lite", "friend_lite"):
            current_provider = "chronicle"

        # Get available providers
        available_providers = ["chronicle", "openmemory_mcp"]

        return {
            "current_provider": current_provider,
            "available_providers": available_providers,
            "status": "success",
        }

    except Exception as e:
        logger.exception("Error getting memory provider")
        raise e


async def set_memory_provider(provider: str):
    """Set memory provider and update .env file."""
    try:
        # Validate provider
        provider = provider.lower().strip()
        valid_providers = ["chronicle", "openmemory_mcp"]

        if provider not in valid_providers:
            raise ValueError(
                f"Invalid provider '{provider}'. Valid providers: {', '.join(valid_providers)}"
            )

        # Path to .env file (assuming we're running from backends/advanced/)
        env_path = os.path.join(os.getcwd(), ".env")

        if not os.path.exists(env_path):
            raise FileNotFoundError(f".env file not found at {env_path}")

        # Read current .env file
        with open(env_path, "r") as file:
            lines = file.readlines()

        # Update or add MEMORY_PROVIDER line
        provider_found = False
        updated_lines = []

        for line in lines:
            if line.strip().startswith("MEMORY_PROVIDER="):
                updated_lines.append(f"MEMORY_PROVIDER={provider}\n")
                provider_found = True
            else:
                updated_lines.append(line)

        # If MEMORY_PROVIDER wasn't found, add it
        if not provider_found:
            updated_lines.append(f"\n# Memory Provider Configuration\nMEMORY_PROVIDER={provider}\n")

        # Create backup
        backup_path = f"{env_path}.bak"
        shutil.copy2(env_path, backup_path)
        logger.info(f"Created .env backup at {backup_path}")

        # Write updated .env file
        with open(env_path, "w") as file:
            file.writelines(updated_lines)

        # Update environment variable for current process
        os.environ["MEMORY_PROVIDER"] = provider

        logger.info(f"Updated MEMORY_PROVIDER to '{provider}' in .env file")

        return {
            "message": f"Memory provider updated to '{provider}'. Please restart the backend service for changes to take effect.",
            "provider": provider,
            "env_path": env_path,
            "backup_created": True,
            "requires_restart": True,
            "status": "success",
        }

    except Exception as e:
        logger.exception("Error setting memory provider")
        raise e


# LLM Operations Configuration Functions


async def get_llm_operations():
    """Get LLM operation configurations and available models."""
    try:
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        # Serialize each LLMOperationConfig to dict
        operations = {}
        for op_name, op_config in registry.llm_operations.items():
            operations[op_name] = {
                "model": op_config.model,
                "temperature": op_config.temperature,
                "max_tokens": op_config.max_tokens,
                "response_format": op_config.response_format,
            }

        # Collect available LLM models
        available_models = [
            {"name": m.name, "description": m.description, "provider": m.model_provider}
            for m in registry.get_all_by_type("llm")
        ]

        default_llm = registry.defaults.get("llm")

        return {
            "operations": operations,
            "available_models": available_models,
            "default_llm": default_llm,
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error getting LLM operations")
        raise e


async def save_llm_operations(operations: dict):
    """Save LLM operation configurations to config.yml and hot-reload."""
    try:
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        valid_keys = {"model", "temperature", "max_tokens", "response_format"}

        for op_name, op_value in operations.items():
            if not isinstance(op_value, dict):
                raise HTTPException(status_code=400, detail=f"Operation '{op_name}' must be a dict")

            extra_keys = set(op_value.keys()) - valid_keys
            if extra_keys:
                raise HTTPException(
                    status_code=400, detail=f"Invalid keys for '{op_name}': {extra_keys}"
                )

            if "temperature" in op_value and op_value["temperature"] is not None:
                t = op_value["temperature"]
                if not isinstance(t, (int, float)) or t < 0 or t > 2:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid temperature for '{op_name}': must be 0-2"
                    )

            if "max_tokens" in op_value and op_value["max_tokens"] is not None:
                mt = op_value["max_tokens"]
                if not isinstance(mt, int) or mt <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid max_tokens for '{op_name}': must be positive int",
                    )

            if "model" in op_value and op_value["model"] is not None:
                if not registry.get_by_name(op_value["model"]):
                    raise HTTPException(
                        status_code=400, detail=f"Model '{op_value['model']}' not found in registry"
                    )

            if "response_format" in op_value and op_value["response_format"] is not None:
                if op_value["response_format"] != "json":
                    raise HTTPException(
                        status_code=400, detail=f"response_format must be 'json' or null"
                    )

        if save_config_section("llm_operations", operations):
            load_models_config(force_reload=True)
            logger.info(f"Updated LLM operations config: {list(operations.keys())}")
            return {
                "message": "LLM operations saved successfully",
                "status": "success",
            }
        else:
            return {
                "message": "Failed to save LLM operations",
                "status": "error",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error saving LLM operations")
        raise e


async def test_llm_model(model_name: Optional[str]):
    """Test an LLM model connection with a trivial prompt."""
    try:
        from advanced_omi_backend.openai_factory import create_openai_client

        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        if model_name:
            model_def = registry.get_by_name(model_name)
            if not model_def:
                return {
                    "success": False,
                    "model_name": model_name,
                    "error": f"Model '{model_name}' not found",
                    "status": "error",
                }
        else:
            model_def = registry.get_default("llm")
            if not model_def:
                return {
                    "success": False,
                    "model_name": None,
                    "error": "No default LLM configured",
                    "status": "error",
                }

        client = create_openai_client(
            api_key=model_def.api_key or "",
            base_url=model_def.model_url,
            is_async=True,
        )

        start = time.time()
        response = await client.chat.completions.create(
            model=model_def.model_name,
            messages=[{"role": "user", "content": "Say hello in one word."}],
            temperature=0,
            max_tokens=10,
        )
        latency_ms = int((time.time() - start) * 1000)

        return {
            "success": True,
            "model_name": model_def.name,
            "model_provider": model_def.model_provider,
            "response": response.choices[0].message.content.strip(),
            "latency_ms": latency_ms,
            "status": "success",
        }
    except Exception as e:
        return {
            "success": False,
            "model_name": model_name or "(default)",
            "error": str(e),
            "status": "error",
        }


# Chat Configuration Management Functions


async def get_chat_config_yaml() -> str:
    """Get chat system prompt as plain text."""
    try:
        config_path = _find_config_path()

        default_prompt = """You are a helpful AI assistant with access to the user's personal memories and conversation history.

Use the provided memories and conversation context to give personalized, contextual responses. If memories are relevant, reference them naturally in your response. Be conversational and helpful.

If no relevant memories are available, respond normally based on the conversation context."""

        if not os.path.exists(config_path):
            return default_prompt

        with open(config_path, "r") as f:
            full_config = _yaml.load(f) or {}

        chat_config = full_config.get("chat", {})
        system_prompt = chat_config.get("system_prompt", default_prompt)

        # Return just the prompt text, not the YAML structure
        return system_prompt

    except Exception as e:
        logger.error(f"Error loading chat config: {e}")
        raise


async def save_chat_config_yaml(prompt_text: str) -> dict:
    """Save chat system prompt from plain text."""
    try:
        config_path = _find_config_path()

        # Validate plain text prompt
        if not prompt_text or not isinstance(prompt_text, str):
            raise ValueError("Prompt must be a non-empty string")

        prompt_text = prompt_text.strip()
        if len(prompt_text) < 10:
            raise ValueError("Prompt too short (minimum 10 characters)")
        if len(prompt_text) > 10000:
            raise ValueError("Prompt too long (maximum 10000 characters)")

        # Create chat config dict
        chat_config = {"system_prompt": prompt_text}

        # Load full config
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                full_config = _yaml.load(f) or {}
        else:
            full_config = {}

        # Backup existing config
        if os.path.exists(config_path):
            backup_path = str(config_path) + ".backup"
            shutil.copy2(config_path, backup_path)
            logger.info(f"Created config backup at {backup_path}")

        # Update chat section
        full_config["chat"] = chat_config

        # Save
        with open(config_path, "w") as f:
            _yaml.dump(full_config, f)

        # Reload config in memory (hot-reload)
        load_models_config(force_reload=True)

        logger.info("Chat configuration updated successfully")

        return {"success": True, "message": "Chat configuration updated successfully"}

    except Exception as e:
        logger.error(f"Error saving chat config: {e}")
        raise


async def validate_chat_config_yaml(prompt_text: str) -> dict:
    """Validate chat system prompt plain text."""
    try:
        # Validate plain text prompt
        if not isinstance(prompt_text, str):
            return {"valid": False, "error": "Prompt must be a string"}

        prompt_text = prompt_text.strip()
        if len(prompt_text) < 10:
            return {"valid": False, "error": "Prompt too short (minimum 10 characters)"}
        if len(prompt_text) > 10000:
            return {"valid": False, "error": "Prompt too long (maximum 10000 characters)"}

        return {"valid": True, "message": "Configuration is valid"}

    except Exception as e:
        logger.error(f"Error validating chat config: {e}")
        return {"valid": False, "error": f"Validation error: {str(e)}"}


# Plugin Configuration Management Functions


async def get_plugins_config_yaml() -> str:
    """Get plugins configuration as YAML text."""
    try:
        plugins_yml_path = get_plugins_yml_path()

        # Default empty plugins config
        default_config = """plugins:
  # No plugins configured yet
  # Example plugin configuration:
  # homeassistant:
  #   enabled: true
  #   access_level: transcript
  #   trigger:
  #     type: wake_word
  #     wake_word: vivi
  #   ha_url: http://localhost:8123
  #   ha_token: YOUR_TOKEN_HERE
"""

        if not plugins_yml_path.exists():
            return default_config

        with open(plugins_yml_path, "r") as f:
            yaml_content = f.read()

        return yaml_content

    except Exception as e:
        logger.error(f"Error loading plugins config: {e}")
        raise


async def save_plugins_config_yaml(yaml_content: str) -> dict:
    """Save plugins configuration from YAML text."""
    try:
        plugins_yml_path = get_plugins_yml_path()

        # Validate YAML can be parsed
        try:
            parsed_config = _yaml.load(yaml_content)
            if not isinstance(parsed_config, dict):
                raise ValueError("Configuration must be a YAML dictionary")

            # Validate has 'plugins' key
            if "plugins" not in parsed_config:
                raise ValueError("Configuration must contain 'plugins' key")

        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid YAML syntax: {e}")

        # Create config directory if it doesn't exist
        plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing config
        if plugins_yml_path.exists():
            backup_path = str(plugins_yml_path) + ".backup"
            shutil.copy2(plugins_yml_path, backup_path)
            logger.info(f"Created plugins config backup at {backup_path}")

        # Save new config
        with open(plugins_yml_path, "w") as f:
            f.write(yaml_content)

        # Hot-reload plugins and signal worker restart
        reload_result = None
        try:
            reload_result, _ = await _reload_and_signal()
            logger.info("Plugins reloaded and worker restart signaled")
        except Exception as reload_err:
            logger.warning(f"Auto-reload failed, manual restart needed: {reload_err}")

        logger.info("Plugins configuration updated successfully")

        message = "Plugins configuration updated and reloaded successfully."
        if reload_result is None:
            message = "Plugins configuration updated. Restart backend for changes to take effect."

        return {
            "success": True,
            "message": message,
            "reload": reload_result,
        }

    except Exception as e:
        logger.error(f"Error saving plugins config: {e}")
        raise


async def validate_plugins_config_yaml(yaml_content: str) -> dict:
    """Validate plugins configuration YAML."""
    try:
        # Parse YAML
        try:
            parsed_config = _yaml.load(yaml_content)
        except Exception as e:
            return {"valid": False, "error": f"Invalid YAML syntax: {e}"}

        # Check structure
        if not isinstance(parsed_config, dict):
            return {"valid": False, "error": "Configuration must be a YAML dictionary"}

        if "plugins" not in parsed_config:
            return {"valid": False, "error": "Configuration must contain 'plugins' key"}

        plugins = parsed_config["plugins"]
        if not isinstance(plugins, dict):
            return {"valid": False, "error": "'plugins' must be a dictionary"}

        # Validate each plugin
        valid_access_levels = ["transcript", "conversation", "memory"]
        valid_trigger_types = ["wake_word", "always", "conditional"]

        for plugin_id, plugin_config in plugins.items():
            if not isinstance(plugin_config, dict):
                return {
                    "valid": False,
                    "error": f"Plugin '{plugin_id}' config must be a dictionary",
                }

            # Check required fields
            if "enabled" in plugin_config and not isinstance(plugin_config["enabled"], bool):
                return {"valid": False, "error": f"Plugin '{plugin_id}': 'enabled' must be boolean"}

            if (
                "access_level" in plugin_config
                and plugin_config["access_level"] not in valid_access_levels
            ):
                return {
                    "valid": False,
                    "error": f"Plugin '{plugin_id}': invalid access_level (must be one of {valid_access_levels})",
                }

            if "trigger" in plugin_config:
                trigger = plugin_config["trigger"]
                if not isinstance(trigger, dict):
                    return {
                        "valid": False,
                        "error": f"Plugin '{plugin_id}': 'trigger' must be a dictionary",
                    }

                if "type" in trigger and trigger["type"] not in valid_trigger_types:
                    return {
                        "valid": False,
                        "error": f"Plugin '{plugin_id}': invalid trigger type (must be one of {valid_trigger_types})",
                    }

        return {"valid": True, "message": "Configuration is valid"}

    except Exception as e:
        logger.error(f"Error validating plugins config: {e}")
        return {"valid": False, "error": f"Validation error: {str(e)}"}


async def _reload_and_signal(app=None) -> tuple[dict, bool]:
    """Reload plugins and signal worker restart.

    Returns:
        (reload_result, worker_signal_sent) tuple.
    """
    from advanced_omi_backend.services.plugin_service import (
        reload_plugins,
        signal_worker_restart,
    )

    reload_result = await reload_plugins(app=app)

    worker_signal_sent = False
    try:
        signal_worker_restart()
        worker_signal_sent = True
    except Exception as e:
        logger.error(f"Failed to signal worker restart: {e}")

    return reload_result, worker_signal_sent


async def restart_workers() -> dict:
    """Signal all RQ workers to gracefully restart via Redis.

    Workers finish their current job before restarting.
    Uses the existing plugin-reload worker restart mechanism.
    """
    from advanced_omi_backend.services.plugin_service import signal_worker_restart

    try:
        signal_worker_restart()
        logger.info("Worker restart signaled via Redis")
        return {
            "message": "Worker restart signal sent. Workers will restart after finishing current jobs.",
            "status": "accepted",
        }
    except Exception as e:
        logger.exception("Failed to signal worker restart")
        raise e


async def restart_backend() -> dict:
    """Schedule a SIGTERM to the current process after a short delay.

    The delay allows the HTTP response to be sent before the process dies.
    Docker (or the process supervisor) will automatically restart the container.
    """

    async def _delayed_kill():
        await asyncio.sleep(1.5)
        logger.info("Sending SIGTERM to self (PID %d) for backend restart", os.getpid())
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_kill())
    logger.info("Backend restart scheduled in 1.5s")
    return {
        "message": "Backend restart scheduled. The service will be briefly unavailable.",
        "status": "accepted",
    }


async def reload_plugins_controller(app=None) -> dict:
    """Reload all plugins and signal workers to restart.

    Args:
        app: Optional FastAPI app instance for updating app.state.plugin_router

    Returns:
        Combined result with backend reload details and worker signal status
    """
    reload_result, worker_signal_sent = await _reload_and_signal(app=app)

    return {
        "success": reload_result.get("success", False),
        "message": (
            "Plugins reloaded and worker restart signaled"
            if worker_signal_sent
            else "Plugins reloaded but worker restart signal failed"
        ),
        "reload": reload_result,
        "worker_signal_sent": worker_signal_sent,
    }


# Structured Plugin Configuration Management Functions (Form-based UI)


async def get_plugins_metadata() -> dict:
    """Get plugin metadata for form-based configuration UI.

    Returns complete metadata for all discovered plugins including:
    - Plugin information (name, description, enabled status)
    - Auto-generated schemas from config.yml (or explicit schema.yml)
    - Current configuration with masked secrets
    - Orchestration settings (events, conditions)

    Returns:
        Dict with plugins list containing metadata for each plugin
    """
    try:
        from advanced_omi_backend.services.plugin_service import (
            discover_plugins,
            get_plugin_metadata,
        )

        # Discover all available plugins
        discovered_plugins = discover_plugins()

        # Load orchestration config from plugins.yml
        plugins_yml_path = get_plugins_yml_path()
        orchestration_configs = {}

        if plugins_yml_path.exists():
            with open(plugins_yml_path, "r") as f:
                plugins_data = _yaml.load(f) or {}
                orchestration_configs = plugins_data.get("plugins", {})

        # Build metadata for each plugin
        plugins_metadata = []
        for plugin_id, plugin_class in discovered_plugins.items():
            # Get orchestration config (or empty dict if not configured)
            orchestration_config = orchestration_configs.get(
                plugin_id, {"enabled": False, "events": [], "condition": {"type": "always"}}
            )

            # Get complete metadata including schema
            metadata = get_plugin_metadata(plugin_id, plugin_class, orchestration_config)
            plugins_metadata.append(metadata)

        logger.info(f"Retrieved metadata for {len(plugins_metadata)} plugins")

        return {"plugins": plugins_metadata, "status": "success"}

    except Exception as e:
        logger.exception("Error getting plugins metadata")
        raise e


async def update_plugin_config_structured(plugin_id: str, config: dict) -> dict:
    """Update plugin configuration from structured JSON (form data).

    Updates the three-file plugin architecture:
    1. config/plugins.yml - Orchestration (enabled, events, condition)
    2. plugins/{plugin_id}/config.yml - Settings with ${ENV_VAR} references
    3. backends/advanced/.env - Actual secret values

    Args:
        plugin_id: Plugin identifier
        config: Structured configuration with 'orchestration', 'settings', 'env_vars' sections

    Returns:
        Success message with list of updated files
    """
    try:
        from advanced_omi_backend.services.plugin_service import (
            _get_plugins_dir,
            discover_plugins,
        )

        # Validate plugin exists
        discovered_plugins = discover_plugins()
        if plugin_id not in discovered_plugins:
            raise ValueError(f"Plugin '{plugin_id}' not found")

        updated_files = []

        # 1. Update config/plugins.yml (orchestration)
        if "orchestration" in config:
            plugins_yml_path = get_plugins_yml_path()

            # Load current plugins.yml
            if plugins_yml_path.exists():
                with open(plugins_yml_path, "r") as f:
                    plugins_data = _yaml.load(f) or {}
            else:
                plugins_data = {}

            if "plugins" not in plugins_data:
                plugins_data["plugins"] = {}

            # Update orchestration config
            orchestration = config["orchestration"]
            plugins_data["plugins"][plugin_id] = {
                "enabled": orchestration.get("enabled", False),
                "events": orchestration.get("events", []),
                "condition": orchestration.get("condition", {"type": "always"}),
            }

            # Create backup
            if plugins_yml_path.exists():
                backup_path = str(plugins_yml_path) + ".backup"
                shutil.copy2(plugins_yml_path, backup_path)

            # Create config directory if needed
            plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

            # Write updated plugins.yml
            with open(plugins_yml_path, "w") as f:
                _yaml.dump(plugins_data, f)

            updated_files.append(str(plugins_yml_path))
            logger.info(f"Updated orchestration config for '{plugin_id}' in {plugins_yml_path}")

        # 2. Update plugins/{plugin_id}/config.yml (settings with env var references)
        if "settings" in config:
            plugins_dir = _get_plugins_dir()
            plugin_config_path = plugins_dir / plugin_id / "config.yml"

            # Load current config.yml
            if plugin_config_path.exists():
                with open(plugin_config_path, "r") as f:
                    plugin_config_data = _yaml.load(f) or {}
            else:
                plugin_config_data = {}

            # Update settings (preserve ${ENV_VAR} references)
            settings = config["settings"]
            plugin_config_data.update(settings)

            # Create backup
            if plugin_config_path.exists():
                backup_path = str(plugin_config_path) + ".backup"
                shutil.copy2(plugin_config_path, backup_path)

            # Write updated config.yml
            with open(plugin_config_path, "w") as f:
                _yaml.dump(plugin_config_data, f)

            updated_files.append(str(plugin_config_path))
            logger.info(f"Updated settings for '{plugin_id}' in {plugin_config_path}")

        # 3. Update per-plugin .env (only changed env vars)
        if "env_vars" in config and config["env_vars"]:
            from advanced_omi_backend.services.plugin_service import save_plugin_env

            # Filter out masked values (unchanged secrets)
            changed_vars = {k: v for k, v in config["env_vars"].items() if v != "••••••••••••"}

            if changed_vars:
                env_path = save_plugin_env(plugin_id, changed_vars)
                updated_files.append(str(env_path))
                logger.info(
                    f"Saved {len(changed_vars)} env var(s) to per-plugin .env for '{plugin_id}'"
                )

                # Update os.environ so hot-reload picks up changes immediately
                for k, v in changed_vars.items():
                    os.environ[k] = v

        # Hot-reload plugins and signal worker restart
        reload_result = None
        try:
            reload_result, _ = await _reload_and_signal()
        except Exception as reload_err:
            logger.warning(f"Auto-reload failed, manual restart needed: {reload_err}")

        message = f"Plugin '{plugin_id}' configuration updated and reloaded successfully."
        if reload_result is None:
            message = f"Plugin '{plugin_id}' configuration updated. Restart backend for changes to take effect."

        return {
            "success": True,
            "message": message,
            "updated_files": updated_files,
            "reload": reload_result,
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error updating structured config for plugin '{plugin_id}'")
        raise e


async def test_plugin_connection(plugin_id: str, config: dict) -> dict:
    """Test plugin connection/configuration without saving.

    Calls the plugin's test_connection method if available to validate
    configuration (e.g., SMTP connection, Home Assistant API).

    Args:
        plugin_id: Plugin identifier
        config: Configuration to test (same structure as update_plugin_config_structured)

    Returns:
        Test result with success status and details
    """
    try:
        from advanced_omi_backend.services.plugin_service import (
            discover_plugins,
            expand_env_vars,
            load_plugin_env,
        )

        # Validate plugin exists
        discovered_plugins = discover_plugins()
        if plugin_id not in discovered_plugins:
            raise ValueError(f"Plugin '{plugin_id}' not found")

        plugin_class = discovered_plugins[plugin_id]

        # Check if plugin supports testing
        if not hasattr(plugin_class, "test_connection"):
            return {
                "success": False,
                "message": f"Plugin '{plugin_id}' does not support connection testing",
                "status": "unsupported",
            }

        # Build complete config from provided data
        test_config = {}

        # Merge settings
        if "settings" in config:
            test_config.update(config["settings"])

        # Load per-plugin env for resolving masked values
        plugin_env = load_plugin_env(plugin_id)

        # Add env vars (expand any ${ENV_VAR} references with test values)
        if "env_vars" in config:
            for key, value in config["env_vars"].items():
                # For masked values, resolve from per-plugin .env then os.environ
                if value == "••••••••••••":
                    value = plugin_env.get(key) or os.getenv(key, "")
                test_config[key.lower()] = value

        # Expand any remaining env var references
        test_config = expand_env_vars(test_config)

        # Call plugin's test_connection static method
        result = await plugin_class.test_connection(test_config)

        logger.info(f"Test connection for '{plugin_id}': {result.get('message', 'No message')}")

        return result

    except Exception as e:
        logger.exception(f"Error testing connection for plugin '{plugin_id}'")
        return {"success": False, "message": f"Connection test failed: {str(e)}", "status": "error"}


# Plugin Lifecycle Management Functions (create / write-code / delete)


def _snake_to_pascal(snake_str: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in snake_str.split("_"))


def _extract_class_name(code: str) -> Optional[str]:
    """Extract the BasePlugin subclass name from plugin code."""
    match = re.search(r"class\s+(\w+)\s*\(.*BasePlugin.*\)", code)
    return match.group(1) if match else None


async def create_plugin(
    plugin_name: str,
    description: str,
    events: list[str],
    plugin_code: Optional[str] = None,
) -> dict:
    """Create a new plugin directory with boilerplate or LLM-generated code.

    Args:
        plugin_name: snake_case plugin identifier
        description: Human-readable description
        events: List of event strings the plugin subscribes to
        plugin_code: Optional full plugin.py source (LLM-generated)

    Returns:
        Success dict with plugin_id and created_files list
    """
    from advanced_omi_backend.services.plugin_service import (
        _get_plugins_dir,
        discover_plugins,
    )

    # Validate name
    if not plugin_name.replace("_", "").isalnum():
        return {"success": False, "error": "Plugin name must be alphanumeric with underscores only"}

    if not re.match(r"^[a-z][a-z0-9_]*$", plugin_name):
        return {
            "success": False,
            "error": "Plugin name must be lowercase snake_case starting with a letter",
        }

    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_name

    # Collision check
    if plugin_dir.exists():
        return {"success": False, "error": f"Plugin '{plugin_name}' already exists at {plugin_dir}"}

    discovered = discover_plugins()
    if plugin_name in discovered:
        return {"success": False, "error": f"Plugin '{plugin_name}' is already registered"}

    class_name = _snake_to_pascal(plugin_name) + "Plugin"
    created_files: list[str] = []

    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # plugin.py
        if plugin_code:
            # Use LLM-generated code; extract real class name from it
            extracted = _extract_class_name(plugin_code)
            if extracted:
                class_name = extracted
            (plugin_dir / "plugin.py").write_text(plugin_code, encoding="utf-8")
        else:
            # Write standard boilerplate
            events_str = (
                ", ".join(f'"{e}"' for e in events) if events else '"conversation.complete"'
            )
            boilerplate = (
                inspect.cleandoc(
                    f'''
                """
                {class_name} implementation.

                {description}
                """
                import logging
                from typing import Any, Dict, Optional

                from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

                logger = logging.getLogger(__name__)


                class {class_name}(BasePlugin):
                    """{description}

                    Subscribes to: [{events_str}]
                    """

                    SUPPORTED_ACCESS_LEVELS = ["conversation"]

                    def __init__(self, config: Dict[str, Any]):
                        super().__init__(config)
                        logger.info("{class_name} loaded")

                    async def initialize(self):
                        if not self.enabled:
                            return
                        logger.info("{class_name} initialized")

                    async def cleanup(self):
                        logger.info("{class_name} cleanup complete")

                    async def on_conversation_complete(self, context: PluginContext) -> Optional[PluginResult]:
                        logger.info(f"Processing conversation for user: {{context.user_id}}")
                        return PluginResult(success=True, message="OK")
            '''
                )
                + "\n"
            )
            (plugin_dir / "plugin.py").write_text(boilerplate, encoding="utf-8")
        created_files.append("plugin.py")

        # __init__.py
        init_content = f'"""{class_name} for Chronicle."""\n\nfrom .plugin import {class_name}\n\n__all__ = ["{class_name}"]\n'
        (plugin_dir / "__init__.py").write_text(init_content, encoding="utf-8")
        created_files.append("__init__.py")

        # config.yml
        config_yml = {"description": description}
        with open(plugin_dir / "config.yml", "w", encoding="utf-8") as f:
            _yaml.dump(config_yml, f)
        created_files.append("config.yml")

        # README.md
        readme = f"# {class_name}\n\n{description}\n"
        (plugin_dir / "README.md").write_text(readme, encoding="utf-8")
        created_files.append("README.md")

        # Add disabled entry to plugins.yml
        plugins_yml_path = get_plugins_yml_path()
        if plugins_yml_path.exists():
            with open(plugins_yml_path, "r") as f:
                plugins_data = _yaml.load(f) or {}
        else:
            plugins_data = {}
            plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

        if "plugins" not in plugins_data:
            plugins_data["plugins"] = {}

        plugins_data["plugins"][plugin_name] = {
            "enabled": False,
            "events": events or ["conversation.complete"],
            "condition": {"type": "always"},
        }
        with open(plugins_yml_path, "w") as f:
            _yaml.dump(plugins_data, f)

        logger.info(f"Created plugin '{plugin_name}' at {plugin_dir}")
        return {
            "success": True,
            "plugin_id": plugin_name,
            "created_files": created_files,
            "plugin_dir": str(plugin_dir),
        }

    except Exception as e:
        # Clean up partial directory on error
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        logger.exception(f"Error creating plugin '{plugin_name}'")
        return {"success": False, "error": str(e)}


async def write_plugin_code(
    plugin_id: str,
    code: str,
    config_yml: Optional[str] = None,
) -> dict:
    """Overwrite an existing plugin's code.

    Args:
        plugin_id: Plugin identifier (directory name)
        code: New plugin.py source code
        config_yml: Optional new config.yml content (YAML string)

    Returns:
        Success dict with updated_files list
    """
    from advanced_omi_backend.services.plugin_service import _get_plugins_dir

    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id

    if not plugin_dir.exists():
        return {"success": False, "error": f"Plugin '{plugin_id}' not found at {plugin_dir}"}

    updated_files: list[str] = []

    try:
        # Write plugin.py
        (plugin_dir / "plugin.py").write_text(code, encoding="utf-8")
        updated_files.append("plugin.py")

        # Update __init__.py with extracted class name
        class_name = _extract_class_name(code)
        if class_name:
            init_content = f'"""{class_name} for Chronicle."""\n\nfrom .plugin import {class_name}\n\n__all__ = ["{class_name}"]\n'
            (plugin_dir / "__init__.py").write_text(init_content, encoding="utf-8")
            updated_files.append("__init__.py")

        # Optionally update config.yml
        if config_yml is not None:
            # Validate YAML
            _yaml.load(config_yml)
            (plugin_dir / "config.yml").write_text(config_yml, encoding="utf-8")
            updated_files.append("config.yml")

        logger.info(f"Updated plugin code for '{plugin_id}': {updated_files}")
        return {
            "success": True,
            "plugin_id": plugin_id,
            "updated_files": updated_files,
        }

    except Exception as e:
        logger.exception(f"Error writing code for plugin '{plugin_id}'")
        return {"success": False, "error": str(e)}


async def delete_plugin(plugin_id: str, remove_files: bool = False) -> dict:
    """Delete a plugin from plugins.yml and optionally remove its files.

    Args:
        plugin_id: Plugin identifier
        remove_files: If True, also delete the plugin directory

    Returns:
        Success dict
    """
    from advanced_omi_backend.services.plugin_service import _get_plugins_dir

    plugins_yml_path = get_plugins_yml_path()

    # Check plugins.yml
    if plugins_yml_path.exists():
        with open(plugins_yml_path, "r") as f:
            plugins_data = _yaml.load(f) or {}
    else:
        plugins_data = {}

    plugin_entry = plugins_data.get("plugins", {}).get(plugin_id)

    # Refuse if enabled
    if plugin_entry and plugin_entry.get("enabled"):
        return {
            "success": False,
            "error": f"Plugin '{plugin_id}' is currently enabled. Disable it first before deleting.",
        }

    # Remove from plugins.yml
    removed_from_yml = False
    if plugin_entry is not None:
        del plugins_data["plugins"][plugin_id]
        with open(plugins_yml_path, "w") as f:
            _yaml.dump(plugins_data, f)
        removed_from_yml = True

    # Optionally remove files
    files_removed = False
    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id
    if remove_files and plugin_dir.exists():
        shutil.rmtree(plugin_dir)
        files_removed = True
        logger.info(f"Removed plugin directory: {plugin_dir}")

    if not removed_from_yml and not files_removed:
        return {
            "success": False,
            "error": f"Plugin '{plugin_id}' not found in plugins.yml or on disk",
        }

    logger.info(f"Deleted plugin '{plugin_id}' (yml={removed_from_yml}, files={files_removed})")
    return {
        "success": True,
        "plugin_id": plugin_id,
        "removed_from_yml": removed_from_yml,
        "files_removed": files_removed,
    }
