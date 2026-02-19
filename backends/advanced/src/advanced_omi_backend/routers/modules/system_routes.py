"""
System and utility routes for Chronicle API.

Handles metrics, auth config, and other system utilities.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user, current_superuser
from advanced_omi_backend.controllers import (
    queue_controller,
    session_controller,
    system_controller,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services import plugin_assistant

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


@router.get("/observability")
async def get_observability_config():
    """Get observability configuration for frontend (Langfuse deep-links).

    Returns non-secret data: enabled status and browser-accessible session URL.
    No authentication required.
    """
    return await system_controller.get_observability_config()


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


# LLM Operations Configuration Endpoints

@router.get("/admin/llm-operations")
async def get_llm_operations(current_user: User = Depends(current_superuser)):
    """Get LLM operation configurations. Admin only."""
    return await system_controller.get_llm_operations()


@router.post("/admin/llm-operations")
async def save_llm_operations(
    operations: dict,
    current_user: User = Depends(current_superuser)
):
    """Save LLM operation configurations. Admin only."""
    return await system_controller.save_llm_operations(operations)


@router.post("/admin/llm-operations/test")
async def test_llm_model(
    model_name: Optional[str] = Body(None, embed=True),
    current_user: User = Depends(current_superuser)
):
    """Test an LLM model connection with a trivial prompt. Admin only."""
    return await system_controller.test_llm_model(model_name)


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

@router.post("/admin/plugins/reload")
async def reload_plugins(
    request: Request,
    current_user: User = Depends(current_superuser),
):
    """Hot-reload all plugins and signal workers to restart. Admin only.

    Reloads plugin code and configuration without a full container restart.
    Workers are signaled asynchronously via Redis and will restart after
    finishing their current job.
    """
    try:
        result = await system_controller.reload_plugins_controller(app=request.app)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Failed to reload plugins: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/system/restart-workers")
async def restart_workers(current_user: User = Depends(current_superuser)):
    """Signal all RQ workers to gracefully restart. Admin only.

    Workers finish their current job before restarting. Safe to run anytime.
    """
    try:
        result = await system_controller.restart_workers()
        return JSONResponse(content=result, status_code=202)
    except Exception as e:
        logger.error(f"Failed to restart workers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/system/restart-backend")
async def restart_backend(current_user: User = Depends(current_superuser)):
    """Schedule a backend restart. Admin only.

    Sends SIGTERM to the FastAPI process after a short delay.
    Docker will automatically restart the container.
    Active WebSocket connections will be dropped.
    """
    try:
        result = await system_controller.restart_backend()
        return JSONResponse(content=result, status_code=202)
    except Exception as e:
        logger.error(f"Failed to schedule backend restart: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/plugins/health")
async def get_plugins_health(current_user: User = Depends(current_superuser)):
    """Get plugin health status for all registered plugins. Admin only."""
    try:
        from advanced_omi_backend.services.plugin_service import get_plugin_router
        plugin_router = get_plugin_router()
        if not plugin_router:
            return {"total": 0, "initialized": 0, "failed": 0, "registered": 0, "plugins": []}
        return plugin_router.get_health_summary()
    except Exception as e:
        logger.error(f"Failed to get plugins health: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/plugins/connectivity")
async def get_plugins_connectivity(current_user: User = Depends(current_superuser)):
    """Live connectivity check for all initialized plugins. Admin only.

    Runs each plugin's health_check() with a 10s timeout and returns results.
    """
    try:
        from advanced_omi_backend.services.plugin_service import get_plugin_router
        plugin_router = get_plugin_router()
        if not plugin_router:
            return {"plugins": {}}
        results = await plugin_router.check_connectivity()
        return {"plugins": results}
    except Exception as e:
        logger.error(f"Failed to check plugin connectivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


class CreatePluginRequest(BaseModel):
    """Request model for creating a new plugin."""
    plugin_name: str
    description: str
    events: list[str] = []
    plugin_code: Optional[str] = None


class WritePluginCodeRequest(BaseModel):
    """Request model for writing plugin code."""
    code: str
    config_yml: Optional[str] = None


class PluginAssistantRequest(BaseModel):
    """Request model for plugin assistant chat."""
    messages: list[dict]


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


@router.post("/admin/plugins/create")
async def create_plugin(
    request: CreatePluginRequest,
    current_user: User = Depends(current_superuser),
):
    """Create a new plugin with boilerplate or LLM-generated code. Admin only."""
    try:
        result = await system_controller.create_plugin(
            plugin_name=request.plugin_name,
            description=request.description,
            events=request.events,
            plugin_code=request.plugin_code,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create plugin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/admin/plugins/{plugin_id}/code")
async def write_plugin_code(
    plugin_id: str,
    request: WritePluginCodeRequest,
    current_user: User = Depends(current_superuser),
):
    """Write or update plugin code. Admin only."""
    try:
        result = await system_controller.write_plugin_code(
            plugin_id=plugin_id,
            code=request.code,
            config_yml=request.config_yml,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to write plugin code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/admin/plugins/{plugin_id}")
async def delete_plugin(
    plugin_id: str,
    remove_files: bool = False,
    current_user: User = Depends(current_superuser),
):
    """Delete a plugin from plugins.yml and optionally remove files. Admin only."""
    try:
        result = await system_controller.delete_plugin(
            plugin_id=plugin_id,
            remove_files=remove_files,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete plugin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/assistant")
async def plugin_assistant_chat(
    body: PluginAssistantRequest,
    current_user: User = Depends(current_superuser),
):
    """AI-powered plugin configuration assistant. Admin only. Returns SSE stream."""
    messages = body.messages

    async def event_stream():
        try:
            async for event in plugin_assistant.generate_response_stream(messages):
                yield f"data: {json.dumps(event, default=str)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Plugin assistant error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'data': {'error': str(e)}})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


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
