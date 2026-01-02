"""
System and utility routes for Chronicle API.

Handles metrics, auth config, and other system utilities.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
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
    return await system_controller.save_diarization_settings(settings)


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
