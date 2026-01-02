"""
Health check routes for Chronicle backend.

This module provides health check endpoints for monitoring the application's status.
"""

import asyncio
import logging
import os
import time
from typing import Dict, Any

import aiohttp
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers.queue_controller import redis_conn
from advanced_omi_backend.client_manager import get_client_manager
from advanced_omi_backend.llm_client import async_health_check
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.model_registry import get_models_registry

# Create router
router = APIRouter(tags=["health"])

# Logging setup
logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")

# MongoDB Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
mongo_client = AsyncIOMotorClient(MONGODB_URI)

# Memory service
memory_service = get_memory_service()

# Transcription provider
transcription_provider = get_transcription_provider()

# Registry-driven configuration for display
REGISTRY = get_models_registry()
if REGISTRY:
    _llm_def = REGISTRY.get_default("llm")
    _embed_def = REGISTRY.get_default("embedding")
    _vs_def = REGISTRY.get_default("vector_store")
else:
    _llm_def = _embed_def = _vs_def = None

QDRANT_BASE_URL = (_vs_def.model_params.get("host") if _vs_def else "qdrant")
QDRANT_PORT = str(_vs_def.model_params.get("port") if _vs_def else "6333")


@router.get("/auth/health")
async def auth_health_check():
    """Pre-flight health check for authentication service connectivity."""
    try:
        # Test database connectivity
        await mongo_client.admin.command("ping")
        
        # Test memory service if available
        if memory_service:
            try:
                await asyncio.wait_for(memory_service.test_connection(), timeout=2.0)
                memory_status = "ok"
            except Exception as e:
                logger.warning(f"Memory service health check failed: {e}")
                memory_status = "degraded"
        else:
            memory_status = "unavailable"
        
        return {
            "status": "ok",
            "database": "ok", 
            "memory_service": memory_status,
            "timestamp": int(time.time())
        }
    except Exception as e:
        logger.error(f"Auth health check failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "detail": "Service connectivity check failed",
                "error_type": "connection_failure",
                "timestamp": int(time.time())
            }
        )


@router.get("/health")
async def health_check():
    """Comprehensive health check for all services."""
    # Load model config once for display fields
    _llm_def = None
    _llm_provider = "openai"
    _llm_model = None
    _llm_base_url = None
    _stt_name = None

    try:
        registry = get_models_registry()
        if registry:
            _defaults = registry.defaults
            _llm_name = _defaults.get("llm")
            _stt_name = _defaults.get("stt")
            _llm_def = registry.models.get(_llm_name)
            _llm_provider = _llm_def.model_provider if _llm_def else "openai"
            _llm_model = _llm_def.model_name if _llm_def else None
            _llm_base_url = _llm_def.model_url if _llm_def else None
    except Exception as e:
        logger.warning(f"Failed to load model config for health check: {e}")
    health_status = {
        "status": "healthy",
        "timestamp": int(time.time()),
        "services": {},
        "config": {
            "mongodb_uri": MONGODB_URI,
            "qdrant_url": f"http://{QDRANT_BASE_URL}:{QDRANT_PORT}",
            "transcription_service": (
                f"Speech to Text ({transcription_provider.name})"
                if transcription_provider
                else "Speech to Text (Not Configured)"
            ),
            "asr_uri": (
                f"{transcription_provider.mode.upper()} ({transcription_provider.name})"
                if transcription_provider
                else "Not configured"
            ),
            "transcription_provider": (
                REGISTRY.get_default("stt").name if REGISTRY and REGISTRY.get_default("stt")
                else "not configured"
            ),

            "provider_type": (
                transcription_provider.mode if transcription_provider else "none"
            ),
            "chunk_dir": str(os.getenv("CHUNK_DIR", "./audio_chunks")),
            "active_clients": get_client_manager().get_client_count(),
            "new_conversation_timeout_minutes": float(os.getenv("NEW_CONVERSATION_TIMEOUT_MINUTES", "1.5")),
            "audio_cropping_enabled": os.getenv("AUDIO_CROPPING_ENABLED", "true").lower() == "true",
            "llm_provider": (_llm_def.model_provider if _llm_def else None),
            "llm_model": (_llm_def.model_name if _llm_def else None),
            "llm_base_url": (_llm_def.model_url if _llm_def else None),
        },
    }

    overall_healthy = True
    critical_services_healthy = True

    # Get configuration once at the start
    # Memory provider (registry-based)
    mem_settings = REGISTRY.memory if REGISTRY else {}
    memory_provider = (mem_settings.get("provider") or "chronicle").lower()

    speaker_service_url = os.getenv("SPEAKER_SERVICE_URL")
    openmemory_mcp_url = os.getenv("OPENMEMORY_MCP_URL")

    # Check MongoDB (critical service)
    try:
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=5.0)
        health_status["services"]["mongodb"] = {
            "status": "✅ Connected",
            "healthy": True,
            "critical": True,
        }
    except asyncio.TimeoutError:
        health_status["services"]["mongodb"] = {
            "status": "❌ Connection Timeout (5s)",
            "healthy": False,
            "critical": True,
        }
        overall_healthy = False
        critical_services_healthy = False
    except Exception as e:
        health_status["services"]["mongodb"] = {
            "status": f"❌ Connection Failed: {str(e)}",
            "healthy": False,
            "critical": True,
        }
        overall_healthy = False
        critical_services_healthy = False

    # Check Redis and RQ Workers (critical for queue processing)
    try:
        from advanced_omi_backend.controllers.queue_controller import get_queue_health

        # Get queue health (includes Redis connection test and worker count)
        queue_health = await asyncio.wait_for(
            asyncio.to_thread(get_queue_health), timeout=5.0
        )

        # Check if Redis is healthy
        redis_healthy = queue_health.get("redis_connection") == "healthy"
        worker_count = queue_health.get("total_workers", 0)
        active_workers = queue_health.get("active_workers", 0)
        idle_workers = queue_health.get("idle_workers", 0)

        if redis_healthy:
            health_status["services"]["redis"] = {
                "status": "✅ Connected",
                "healthy": True,
                "critical": True,
                "worker_count": worker_count,
                "active_workers": active_workers,
                "idle_workers": idle_workers,
                "queues": queue_health.get("queues", {})
            }
        else:
            health_status["services"]["redis"] = {
                "status": f"❌ Connection Failed: {queue_health.get('redis_connection')}",
                "healthy": False,
                "critical": True,
                "worker_count": 0
            }
            overall_healthy = False
            critical_services_healthy = False

    except asyncio.TimeoutError:
        health_status["services"]["redis"] = {
            "status": "❌ Connection Timeout (5s)",
            "healthy": False,
            "critical": True,
            "worker_count": 0
        }
        overall_healthy = False
        critical_services_healthy = False
    except Exception as e:
        health_status["services"]["redis"] = {
            "status": f"❌ Connection Failed: {str(e)}",
            "healthy": False,
            "critical": True,
            "worker_count": 0
        }
        overall_healthy = False
        critical_services_healthy = False

    # Check LLM service (non-critical service - may not be running)
    try:
        llm_health = await asyncio.wait_for(async_health_check(), timeout=8.0)
        health_status["services"]["audioai"] = {
            "status": llm_health.get("status", "❌ Unknown"),
            "healthy": "✅" in llm_health.get("status", ""),
            "base_url": llm_health.get("base_url", ""),
            "model": llm_health.get("default_model", ""),
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
    except asyncio.TimeoutError:
        health_status["services"]["audioai"] = {
            "status": "⚠️ Connection Timeout (8s) - Service may not be running",
            "healthy": False,
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
        overall_healthy = False
    except Exception as e:
        health_status["services"]["audioai"] = {
            "status": f"⚠️ Connection Failed: {str(e)} - Service may not be running",
            "healthy": False,
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
        overall_healthy = False

    # Check memory service (provider-dependent)
    if memory_provider == "chronicle":
        try:
            # Test Chronicle memory service connection with timeout
            test_success = await asyncio.wait_for(memory_service.test_connection(), timeout=8.0)
            if test_success:
                health_status["services"]["memory_service"] = {
                    "status": "✅ Chronicle Memory Connected",
                    "healthy": True,
                    "provider": "chronicle",
                    "critical": False,
                }
            else:
                health_status["services"]["memory_service"] = {
                    "status": "⚠️ Chronicle Memory Test Failed",
                    "healthy": False,
                    "provider": "chronicle",
                    "critical": False,
                }
                overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["memory_service"] = {
                "status": "⚠️ Chronicle Memory Timeout (8s) - Check Qdrant",
                "healthy": False,
                "provider": "chronicle",
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["memory_service"] = {
                "status": f"⚠️ Chronicle Memory Failed: {str(e)}",
                "healthy": False,
                "provider": "chronicle",
                "critical": False,
            }
            overall_healthy = False
    elif memory_provider == "openmemory_mcp":
        # OpenMemory MCP check is handled separately above
        health_status["services"]["memory_service"] = {
            "status": "✅ Using OpenMemory MCP",
            "healthy": True,
            "provider": "openmemory_mcp",
            "critical": False,
        }
    elif memory_provider == "mycelia":
        # Mycelia memory service check
        try:
            # Test Mycelia memory service connection with timeout
            test_success = await asyncio.wait_for(memory_service.test_connection(), timeout=8.0)
            if test_success:
                health_status["services"]["memory_service"] = {
                    "status": "✅ Mycelia Memory Connected",
                    "healthy": True,
                    "provider": "mycelia",
                    "critical": False,
                }
            else:
                health_status["services"]["memory_service"] = {
                    "status": "⚠️ Mycelia Memory Test Failed",
                    "healthy": False,
                    "provider": "mycelia",
                    "critical": False,
                }
                overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["memory_service"] = {
                "status": "⚠️ Mycelia Memory Timeout (8s) - Check Mycelia service",
                "healthy": False,
                "provider": "mycelia",
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["memory_service"] = {
                "status": f"⚠️ Mycelia Memory Failed: {str(e)}",
                "healthy": False,
                "provider": "mycelia",
                "critical": False,
            }
            overall_healthy = False
    else:
        health_status["services"]["memory_service"] = {
            "status": f"❌ Unknown memory provider: {memory_provider}",
            "healthy": False,
            "provider": memory_provider,
            "critical": False,
        }
        overall_healthy = False

    # Check Speech to Text service based on configured provider
    if transcription_provider:
        provider_name = transcription_provider.name
        provider_type = transcription_provider.mode

        # Generic provider health check - let each provider handle its own connection logic
        try:
            # Test provider connection
            await transcription_provider.connect("health-check")
            await transcription_provider.disconnect()

            health_status["services"]["speech_to_text"] = {
                "status": "✅ Provider Available",
                "healthy": True,
                "type": provider_type.title(),
                "provider": provider_name,
                "critical": False,
            }
        except Exception as e:
            health_status["services"]["speech_to_text"] = {
                "status": f"⚠️ Provider Error: {str(e)}",
                "healthy": False,
                "type": provider_type.title(),
                "provider": provider_name,
                "critical": False,
            }
            # Don't mark overall health as unhealthy for transcription issues
            # since the service may be external or optional
    else:
        # No transcription service configured
        health_status["services"]["speech_to_text"] = {
            "status": "❌ No transcription service configured",
            "healthy": False,
            "type": "None",
            "provider": "None",
            "critical": False,
        }
        overall_healthy = False

    # Check Speaker Recognition service (non-critical - optional feature)
    if speaker_service_url:
        try:
            # Make a health check request to the speaker service
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{speaker_service_url}/health", timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        health_status["services"]["speaker_recognition"] = {
                            "status": "✅ Connected",
                            "healthy": True,
                            "url": speaker_service_url,
                            "critical": False,
                        }
                    else:
                        health_status["services"]["speaker_recognition"] = {
                            "status": f"⚠️ Unhealthy: HTTP {response.status}",
                            "healthy": False,
                            "url": speaker_service_url,
                            "critical": False,
                        }
                        overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["speaker_recognition"] = {
                "status": "⚠️ Connection Timeout (5s)",
                "healthy": False,
                "url": speaker_service_url,
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["speaker_recognition"] = {
                "status": f"⚠️ Connection Failed: {str(e)}",
                "healthy": False,
                "url": speaker_service_url,
                "critical": False,
            }
            overall_healthy = False

    # Check OpenMemory MCP service (if configured)
    if memory_provider == "openmemory_mcp" and openmemory_mcp_url:
        try:
            # Make a health check request to the OpenMemory MCP service
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{openmemory_mcp_url}/api/v1/apps/", timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        health_status["services"]["openmemory_mcp"] = {
                            "status": "✅ Connected",
                            "healthy": True,
                            "url": openmemory_mcp_url,
                            "provider": "openmemory_mcp",
                            "critical": False,
                        }
                    else:
                        health_status["services"]["openmemory_mcp"] = {
                            "status": f"⚠️ Unhealthy: HTTP {response.status}",
                            "healthy": False,
                            "url": openmemory_mcp_url,
                            "provider": "openmemory_mcp",
                            "critical": False,
                        }
                        overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["openmemory_mcp"] = {
                "status": "⚠️ Connection Timeout (5s)",
                "healthy": False,
                "url": openmemory_mcp_url,
                "provider": "openmemory_mcp",
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["openmemory_mcp"] = {
                "status": f"⚠️ Connection Failed: {str(e)}",
                "healthy": False,
                "url": openmemory_mcp_url,
                "provider": "openmemory_mcp",
                "critical": False,
            }
            overall_healthy = False

    # Set overall status
    health_status["overall_healthy"] = overall_healthy
    health_status["critical_services_healthy"] = critical_services_healthy

    if not critical_services_healthy:
        health_status["status"] = "critical"
    elif not overall_healthy:
        health_status["status"] = "degraded"
    else:
        health_status["status"] = "healthy"

    # Add helpful messages
    if not overall_healthy:
        messages = []
        if not critical_services_healthy:
            messages.append(
                "Critical services (MongoDB) are unavailable - core functionality will not work"
            )

        unhealthy_optional = [
            name
            for name, service in health_status["services"].items()
            if not service["healthy"] and not service.get("critical", True)
        ]
        if unhealthy_optional:
            messages.append(f"Optional services unavailable: {', '.join(unhealthy_optional)}")

        health_status["message"] = "; ".join(messages)

    return JSONResponse(content=health_status, status_code=200)


@router.get("/readiness")
async def readiness_check():
    """Simple readiness check for container orchestration."""
    # Use debug level for health check to reduce log spam
    logger.debug("Readiness check requested")
    
    # Only check critical services for readiness
    try:
        # Quick MongoDB ping to ensure we can serve requests
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=2.0)
        return JSONResponse(content={"status": "ready", "timestamp": int(time.time())}, status_code=200)
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return JSONResponse(
            content={"status": "not_ready", "error": str(e), "timestamp": int(time.time())}, 
            status_code=503
        )
