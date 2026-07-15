"""
Router modules for Chronicle API.

This package contains organized router modules for different functional areas:
- user_routes: User management and authentication
- chat_routes: Chat interface with memory integration
- client_routes: Active client monitoring and management
- conversation_routes: Conversation CRUD and audio processing
- memory_routes: Memory management, search, and debug
- annotation_routes: Annotation CRUD for memories and transcripts
- finetuning_routes: Model fine-tuning and training management
- system_routes: System utilities and metrics
- queue_routes: Job queue management and monitoring
- audio_routes: Audio file uploads and processing
- health_routes: Health check endpoints
- websocket_routes: WebSocket connection handling
- admin_routes: Admin-only system management endpoints
- sse_routes: Server-Sent Events for real-time UI updates
"""

from .admin_routes import router as admin_router
from .annotation_routes import router as annotation_router
from .audio_routes import router as audio_router
from .chat_routes import router as chat_router
from .client_routes import router as client_router
from .conversation_routes import router as conversation_router
from .data_audit_routes import router as data_audit_router
from .finetuning_routes import router as finetuning_router
from .health_routes import router as health_router
from .memory_routes import router as memory_router
from .openai_compat_routes import router as openai_compat_router
from .queue_routes import router as queue_router
from .sse_routes import router as sse_router
from .system_events_routes import router as system_events_router
from .system_routes import router as system_router
from .user_routes import router as user_router
from .vault_sync_routes import router as vault_sync_router
from .wakeword_routes import router as wakeword_router
from .websocket_routes import router as websocket_router

__all__ = [
    "admin_router",
    "annotation_router",
    "audio_router",
    "chat_router",
    "client_router",
    "conversation_router",
    "data_audit_router",
    "finetuning_router",
    "health_router",
    "memory_router",
    "openai_compat_router",
    "queue_router",
    "sse_router",
    "system_events_router",
    "system_router",
    "user_router",
    "vault_sync_router",
    "wakeword_router",
    "websocket_router",
]
