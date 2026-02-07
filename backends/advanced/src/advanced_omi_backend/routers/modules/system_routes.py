"""
System and utility routes for Chronicle API.

Handles metrics, auth config, and other system utilities.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user, current_superuser
from advanced_omi_backend.controllers import (
    queue_controller,
    session_controller,
    system_controller,
)
from advanced_omi_backend.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


# Request models for memory config endpoints
class MemoryConfigRequest(BaseModel):
    """Request model for memory configuration validation and updates."""
    config_yaml: str


@router.get("/config/diagnostics")
async def get_config_diagnostics(current_user: User = Depends(current_superuser)):
    """Get configuration diagnostics including errors, warnings, and status. Admin only."""
    return await system_controller.get_config_diagnostics()


@router.get("/metrics")
async def get_current_metrics(current_user: User = Depends(current_superuser)):
    """Get current system metrics. Admin only."""
    return await system_controller.get_current_metrics()


@router.get("/auth/config")
async def get_auth_config():
    """Get authentication configuration for frontend."""
    return await system_controller.get_auth_config()


@router.get("/diarization-settings")
async def get_diarization_settings(current_user: User = Depends(current_superuser)):
    """Get current diarization settings. Admin only."""
    return await system_controller.get_diarization_settings()


@router.post("/diarization-settings")
async def save_diarization_settings(
    settings: dict,
    current_user: User = Depends(current_superuser)
):
    """Save diarization settings. Admin only."""
    return await system_controller.save_diarization_settings_controller(settings)


@router.get("/misc-settings")
async def get_misc_settings(current_user: User = Depends(current_superuser)):
    """Get miscellaneous configuration settings. Admin only."""
    return await system_controller.get_misc_settings()


@router.post("/misc-settings")
async def save_misc_settings(
    settings: dict,
    current_user: User = Depends(current_superuser)
):
    """Save miscellaneous configuration settings. Admin only."""
    return await system_controller.save_misc_settings_controller(settings)


@router.get("/cleanup-settings")
async def get_cleanup_settings(
    current_user: User = Depends(current_superuser)
):
    """Get cleanup configuration settings. Admin only."""
    return await system_controller.get_cleanup_settings_controller(current_user)


@router.post("/cleanup-settings")
async def save_cleanup_settings(
    auto_cleanup_enabled: bool = Body(..., description="Enable automatic cleanup of soft-deleted conversations"),
    retention_days: int = Body(..., ge=1, le=365, description="Number of days to keep soft-deleted conversations"),
    current_user: User = Depends(current_superuser)
):
    """Save cleanup configuration settings. Admin only."""
    return await system_controller.save_cleanup_settings_controller(
        auto_cleanup_enabled=auto_cleanup_enabled,
        retention_days=retention_days,
        user=current_user
    )


@router.get("/speaker-configuration")
async def get_speaker_configuration(current_user: User = Depends(current_active_user)):
    """Get current user's primary speakers configuration."""
    return await system_controller.get_speaker_configuration(current_user)


@router.post("/speaker-configuration")
async def update_speaker_configuration(
    primary_speakers: list[dict],
    current_user: User = Depends(current_active_user)
):
    """Update current user's primary speakers configuration."""
    return await system_controller.update_speaker_configuration(current_user, primary_speakers)


@router.get("/enrolled-speakers")
async def get_enrolled_speakers(current_user: User = Depends(current_active_user)):
    """Get enrolled speakers from speaker recognition service."""
    return await system_controller.get_enrolled_speakers(current_user)


@router.get("/speaker-service-status")
async def get_speaker_service_status(current_user: User = Depends(current_superuser)):
    """Check speaker recognition service health status. Admin only."""
    return await system_controller.get_speaker_service_status()


# Memory Configuration Management Endpoints Removed - Project uses config.yml exclusively
@router.get("/admin/memory/config/raw")
async def get_memory_config_raw(current_user: User = Depends(current_superuser)):
    """Get memory configuration YAML from config.yml. Admin only."""
    return await system_controller.get_memory_config_raw()

@router.post("/admin/memory/config/raw")
async def update_memory_config_raw(
    config_yaml: str = Body(..., media_type="text/plain"),
    current_user: User = Depends(current_superuser)
):
    """Save memory YAML to config.yml and hot-reload. Admin only."""
    return await system_controller.update_memory_config_raw(config_yaml)


@router.post("/admin/memory/config/validate/raw")
async def validate_memory_config_raw(
    config_yaml: str = Body(..., media_type="text/plain"),
    current_user: User = Depends(current_superuser),
):
    """Validate posted memory YAML as plain text (used by Web UI). Admin only."""
    return await system_controller.validate_memory_config(config_yaml)


@router.post("/admin/memory/config/validate")
async def validate_memory_config(
    request: MemoryConfigRequest,
    current_user: User = Depends(current_superuser)
):
    """Validate memory configuration YAML sent as JSON (used by tests). Admin only."""
    return await system_controller.validate_memory_config(request.config_yaml)


@router.post("/admin/memory/config/reload")
async def reload_memory_config(current_user: User = Depends(current_superuser)):
    """Reload memory configuration from config.yml. Admin only."""
    return await system_controller.reload_memory_config()


@router.delete("/admin/memory/delete-all")
async def delete_all_user_memories(current_user: User = Depends(current_active_user)):
    """Delete all memories for the current user."""
    return await system_controller.delete_all_user_memories(current_user)


# Chat Configuration Management Endpoints

@router.get("/admin/chat/config", response_class=Response)
async def get_chat_config(current_user: User = Depends(current_superuser)):
    """Get chat configuration as YAML. Admin only."""
    try:
        yaml_content = await system_controller.get_chat_config_yaml()
        return Response(content=yaml_content, media_type="text/plain")
    except Exception as e:
        logger.error(f"Failed to get chat config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/chat/config")
async def save_chat_config(
    request: Request,
    current_user: User = Depends(current_superuser)
):
    """Save chat configuration from YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode('utf-8')
        result = await system_controller.save_chat_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to save chat config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/chat/config/validate")
async def validate_chat_config(
    request: Request,
    current_user: User = Depends(current_superuser)
):
    """Validate chat configuration YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode('utf-8')
        result = await system_controller.validate_chat_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Failed to validate chat config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Plugin Configuration Management Endpoints

@router.get("/admin/plugins/config", response_class=Response)
async def get_plugins_config(current_user: User = Depends(current_superuser)):
    """Get plugins configuration as YAML. Admin only."""
    try:
        yaml_content = await system_controller.get_plugins_config_yaml()
        return Response(content=yaml_content, media_type="text/plain")
    except Exception as e:
        logger.error(f"Failed to get plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/config")
async def save_plugins_config(
    request: Request,
    current_user: User = Depends(current_superuser)
):
    """Save plugins configuration from YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode('utf-8')
        result = await system_controller.save_plugins_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to save plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/config/validate")
async def validate_plugins_config(
    request: Request,
    current_user: User = Depends(current_superuser)
):
    """Validate plugins configuration YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode('utf-8')
        result = await system_controller.validate_plugins_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Failed to validate plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Structured Plugin Configuration Endpoints (Form-based UI)

@router.get("/admin/plugins/metadata")
async def get_plugins_metadata(current_user: User = Depends(current_superuser)):
    """Get plugin metadata for form-based configuration UI. Admin only.

    Returns:
        - Plugin information (name, description, enabled status)
        - Auto-generated schemas from config.yml
        - Current configuration with masked secrets
        - Orchestration settings (events, conditions)
    """
    try:
        return await system_controller.get_plugins_metadata()
    except Exception as e:
        logger.error(f"Failed to get plugins metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class PluginConfigRequest(BaseModel):
    """Request model for structured plugin configuration updates."""
    orchestration: Optional[dict] = None
    settings: Optional[dict] = None
    env_vars: Optional[dict] = None


@router.post("/admin/plugins/config/structured/{plugin_id}")
async def update_plugin_config_structured(
    plugin_id: str,
    config: PluginConfigRequest,
    current_user: User = Depends(current_superuser)
):
    """Update plugin configuration from structured JSON (form data). Admin only.

    Updates the three-file plugin architecture:
    1. config/plugins.yml - Orchestration (enabled, events, condition)
    2. plugins/{plugin_id}/config.yml - Settings with ${ENV_VAR} references
    3. backends/advanced/.env - Actual secret values
    """
    try:
        config_dict = config.dict(exclude_none=True)
        result = await system_controller.update_plugin_config_structured(plugin_id, config_dict)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update plugin config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/test-connection/{plugin_id}")
async def test_plugin_connection(
    plugin_id: str,
    config: PluginConfigRequest,
    current_user: User = Depends(current_superuser)
):
    """Test plugin connection/configuration without saving. Admin only.

    Calls the plugin's test_connection method to validate configuration
    (e.g., SMTP connection, Home Assistant API).
    """
    try:
        config_dict = config.dict(exclude_none=True)
        result = await system_controller.test_plugin_connection(plugin_id, config_dict)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to test plugin connection: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/streaming/status")
async def get_streaming_status(request: Request, current_user: User = Depends(current_superuser)):
    """Get status of active streaming sessions and Redis Streams health. Admin only."""
    return await session_controller.get_streaming_status(request)


@router.post("/streaming/cleanup")
async def cleanup_stuck_stream_workers(request: Request, current_user: User = Depends(current_superuser)):
    """Clean up stuck Redis Stream workers and pending messages. Admin only."""
    return await queue_controller.cleanup_stuck_stream_workers(request)


@router.post("/streaming/cleanup-sessions")
async def cleanup_old_sessions(request: Request, max_age_seconds: int = 3600, current_user: User = Depends(current_superuser)):
    """Clean up old session tracking metadata. Admin only."""
    return await session_controller.cleanup_old_sessions(request, max_age_seconds)


# Memory Provider Configuration Endpoints

@router.get("/admin/memory/provider")
async def get_memory_provider(current_user: User = Depends(current_superuser)):
    """Get current memory provider configuration. Admin only."""
    return await system_controller.get_memory_provider()


@router.post("/admin/memory/provider")
async def set_memory_provider(
    provider: str = Body(..., embed=True),
    current_user: User = Depends(current_superuser)
):
    """Set memory provider and restart backend services. Admin only."""
    return await system_controller.set_memory_provider(provider)


# ── Prompt Management ──────────────────────────────────────────────────────
# Prompt editing is now handled via the LangFuse web UI at http://localhost:3002/prompts
