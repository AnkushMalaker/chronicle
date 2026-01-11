"""
System controller for handling system-related business logic.
"""

import logging
import os
import shutil
import time
from datetime import UTC, datetime

import yaml
from fastapi import HTTPException

from advanced_omi_backend.config import (
    get_config,
    get_config_path,
    load_diarization_settings_from_file,
    reload_config,
    save_diarization_settings_to_file,
)
from advanced_omi_backend.model_registry import load_models_config
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


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


# Audio file processing functions moved to audio_controller.py


# Configuration functions moved to config.py to avoid circular imports


async def get_diarization_settings():
    """Get current diarization settings."""
    try:
        # Reload from file to get latest settings
        settings = load_diarization_settings_from_file()
        return {
            "settings": settings,
            "status": "success"
        }
    except Exception as e:
        logger.exception("Error getting diarization settings")
        raise e


async def save_diarization_settings(settings: dict):
    """Save diarization settings."""
    try:
        # Validate settings
        valid_keys = {
            "diarization_source", "similarity_threshold", "min_duration", "collar",
            "min_duration_off", "min_speakers", "max_speakers"
        }

        for key, value in settings.items():
            if key not in valid_keys:
                raise HTTPException(status_code=400, detail=f"Invalid setting key: {key}")

            # Type validation
            if key in ["min_speakers", "max_speakers"]:
                if not isinstance(value, int) or value < 1 or value > 20:
                    raise HTTPException(status_code=400, detail=f"Invalid value for {key}: must be integer 1-20")
            elif key == "diarization_source":
                if not isinstance(value, str) or value not in ["pyannote", "deepgram"]:
                    raise HTTPException(status_code=400, detail=f"Invalid value for {key}: must be 'pyannote' or 'deepgram'")
            else:
                if not isinstance(value, (int, float)) or value < 0:
                    raise HTTPException(status_code=400, detail=f"Invalid value for {key}: must be positive number")
        
        # Get current settings and merge with new values
        current_settings = load_diarization_settings_from_file()
        current_settings.update(settings)
        
        # Save to file
        if save_diarization_settings_to_file(current_settings):
            logger.info(f"Updated and saved diarization settings: {settings}")
            
            return {
                "message": "Diarization settings saved successfully",
                "settings": current_settings,
                "status": "success"
            }
        else:
            # Even if file save fails, we've updated the in-memory settings
            logger.warning("Settings updated in memory but file save failed")
            return {
                "message": "Settings updated (file save failed)",
                "settings": current_settings,
                "status": "partial"
            }
        
    except Exception as e:
        logger.exception("Error saving diarization settings")
        raise e


async def get_speaker_configuration(user: User):
    """Get current user's primary speakers configuration."""
    try:
        return {
            "primary_speakers": user.primary_speakers,
            "user_id": user.user_id,
            "status": "success"
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
        
        logger.info(f"Updated primary speakers configuration for user {user.user_id}: {len(primary_speakers)} speakers")
        
        return {
            "message": "Primary speakers configuration updated successfully",
            "primary_speakers": primary_speakers,
            "count": len(primary_speakers),
            "status": "success"
        }
        
    except Exception as e:
        logger.exception(f"Error updating speaker configuration for user {user.user_id}")
        raise e


async def get_enrolled_speakers(user: User):
    """Get enrolled speakers from speaker recognition service."""
    try:
        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()
        
        if not speaker_client.enabled:
            return {
                "speakers": [],
                "service_available": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "success"
            }
        
        # Get enrolled speakers - using hardcoded user_id=1 for now (as noted in speaker_recognition_client.py)
        speakers = await speaker_client.get_enrolled_speakers(user_id="1")
        
        return {
            "speakers": speakers.get("speakers", []) if speakers else [],
            "service_available": True,
            "message": "Successfully retrieved enrolled speakers",
            "status": "success"
        }
        
    except Exception as e:
        logger.exception(f"Error getting enrolled speakers for user {user.user_id}")
        raise e


async def get_speaker_service_status():
    """Check speaker recognition service health status."""
    try:
        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()
        
        if not speaker_client.enabled:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "disabled"
            }
        
        # Perform health check
        health_result = await speaker_client.health_check()
        
        if health_result:
            return {
                "service_available": True,
                "healthy": True,
                "message": "Speaker recognition service is healthy",
                "service_url": speaker_client.service_url,
                "status": "healthy"
            }
        else:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not responding",
                "service_url": speaker_client.service_url,
                "status": "unhealthy"
            }
        
    except Exception as e:
        logger.exception("Error checking speaker service status")
        raise e



# Memory Configuration Management Functions

async def get_memory_config_raw():
    """Get current memory configuration (memory section of merged config) as YAML.

    Returns the merged configuration from defaults.yml + config.yml + env vars,
    which represents the actual runtime configuration.

    Falls back to empty memory config if configuration cannot be loaded.
    """
    try:
        # Get merged configuration (defaults + config.yml + env vars)
        merged_config = get_config()
        memory_section = merged_config.get("memory", {})
        config_yaml = yaml.safe_dump(memory_section, sort_keys=False)

        cfg_path = get_config_path()
        return {
            "config_yaml": config_yaml,
            "config_path": str(cfg_path),
            "section": "memory",
            "status": "success",
        }
    except Exception as e:
        logger.warning(f"Error reading memory config, using empty config: {e}")
        # Return empty memory config as fallback
        cfg_path = get_config_path()
        return {
            "config_yaml": yaml.safe_dump({}, sort_keys=False),
            "config_path": str(cfg_path),
            "section": "memory",
            "status": "fallback",
        }


async def update_memory_config_raw(config_yaml: str):
    """Update memory configuration in config.yml and hot reload registry."""
    try:
        # Validate YAML
        try:
            new_mem = yaml.safe_load(config_yaml) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML syntax: {str(e)}")

        cfg_path = get_config_path()

        # Backup existing config if it exists
        backup_created = False
        if os.path.exists(cfg_path):
            backup_path = f"{cfg_path}.bak"
            shutil.copy2(cfg_path, backup_path)
            backup_created = True
            logger.info(f"Created config backup at {backup_path}")

        # Load current config.yml (or start with empty dict)
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r') as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
            logger.info(f"Creating new config.yml at {cfg_path}")

        # Update memory section
        data["memory"] = new_mem

        # Write to config.yml
        with open(cfg_path, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False)

        # Reload both config cache and model registry
        reload_config()  # Reload central config cache
        load_models_config(force_reload=True)  # Reload model registry

        return {
            "message": "Memory configuration updated and reloaded successfully",
            "config_path": str(cfg_path),
            "backup_created": backup_created,
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error updating memory config")
        raise e


async def validate_memory_config(config_yaml: str):
    """Validate memory configuration YAML syntax (memory section)."""
    try:
        try:
            parsed = yaml.safe_load(config_yaml)
        except yaml.YAMLError as e:
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
        cfg_path = get_config_path()
        load_models_config(force_reload=True)
        return {"message": "Configuration reloaded", "config_path": str(cfg_path), "status": "success"}
    except Exception as e:
        logger.exception("Error reloading config")
        raise e


async def delete_all_user_memories(user: User):
    """Delete all memories for the current user."""
    try:
        memory_service = get_memory_service()

        # Delete all memories for the user
        deleted_count = await memory_service.delete_all_user_memories(user.user_id)

        logger.info(f"Deleted {deleted_count} memories for user {user.user_id}")

        return {
            "message": f"Successfully deleted {deleted_count} memories",
            "deleted_count": deleted_count,
            "user_id": user.user_id,
            "status": "success"
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
        available_providers = ["chronicle", "openmemory_mcp", "mycelia"]

        return {
            "current_provider": current_provider,
            "available_providers": available_providers,
            "status": "success"
        }

    except Exception as e:
        logger.exception("Error getting memory provider")
        raise e


async def set_memory_provider(provider: str):
    """Set memory provider and update .env file."""
    try:
        # Validate provider
        provider = provider.lower().strip()
        valid_providers = ["chronicle", "openmemory_mcp", "mycelia"]

        if provider not in valid_providers:
            raise ValueError(f"Invalid provider '{provider}'. Valid providers: {', '.join(valid_providers)}")

        # Path to .env file (assuming we're running from backends/advanced/)
        env_path = os.path.join(os.getcwd(), ".env")

        if not os.path.exists(env_path):
            raise FileNotFoundError(f".env file not found at {env_path}")

        # Read current .env file
        with open(env_path, 'r') as file:
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
        with open(env_path, 'w') as file:
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
            "status": "success"
        }

    except Exception as e:
        logger.exception("Error setting memory provider")
        raise e


# Chat Configuration Management Functions

async def get_chat_config_yaml() -> str:
    """Get chat system prompt as plain text from merged configuration.

    Returns the merged configuration from defaults.yml + config.yml + env vars,
    which represents the actual runtime configuration.

    Falls back to default prompt if configuration cannot be loaded.
    """
    default_prompt = """You are a helpful AI assistant with access to the user's personal memories and conversation history.

Use the provided memories and conversation context to give personalized, contextual responses. If memories are relevant, reference them naturally in your response. Be conversational and helpful.

If no relevant memories are available, respond normally based on the conversation context."""

    try:
        # Get merged configuration (defaults + config.yml + env vars)
        merged_config = get_config()
        chat_config = merged_config.get('chat', {})
        system_prompt = chat_config.get('system_prompt', default_prompt)

        # Return just the prompt text, not the YAML structure
        return system_prompt

    except Exception as e:
        logger.warning(f"Error loading chat config, using default prompt: {e}")
        return default_prompt


async def save_chat_config_yaml(prompt_text: str) -> dict:
    """Save chat system prompt from plain text."""
    try:
        config_path = get_config_path()

        # Validate plain text prompt
        if not prompt_text or not isinstance(prompt_text, str):
            raise ValueError("Prompt must be a non-empty string")

        prompt_text = prompt_text.strip()
        if len(prompt_text) < 10:
            raise ValueError("Prompt too short (minimum 10 characters)")
        if len(prompt_text) > 10000:
            raise ValueError("Prompt too long (maximum 10000 characters)")

        # Create chat config dict
        chat_config = {'system_prompt': prompt_text}

        # Backup existing config if it exists
        if os.path.exists(config_path):
            backup_path = str(config_path) + '.backup'
            shutil.copy2(config_path, backup_path)
            logger.info(f"Created config backup at {backup_path}")

        # Load current config.yml (not merged - we only write to config.yml)
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                full_config = yaml.safe_load(f) or {}
        else:
            full_config = {}
            logger.info(f"Creating new config.yml at {config_path}")

        # Update chat section
        full_config['chat'] = chat_config

        # Save to config.yml
        with open(config_path, 'w') as f:
            yaml.dump(full_config, f, default_flow_style=False, allow_unicode=True)

        # Reload both model registry and config cache (hot-reload)
        reload_config()  # Reload central config cache
        load_models_config(force_reload=True)  # Reload model registry

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
