"""Shared configuration and auth for Chronicle's native client apps.

Used by chronicle-tray, vault sync, the wearable client and the havpe relay so
each one stops re-deriving the backend URL, the repo-root ``.env`` load and the
API-key handling for itself.
"""

from chronicle_client.auth import (
    acheck_credentials,
    auth_headers,
    bearer_query_param,
    check_credentials,
    describe_key,
)
from chronicle_client.config import (
    REPO_ROOT,
    ClientConfig,
    load_client_env,
    resolve_backend_url,
    websocket_url,
)
from chronicle_client.voice_session import (
    ServerUpgradeRequired,
    VoiceTargetCapabilities,
    WearableVoiceProtocolError,
    WearableVoiceSession,
)

__all__ = [
    "ClientConfig",
    "REPO_ROOT",
    "ServerUpgradeRequired",
    "VoiceTargetCapabilities",
    "WearableVoiceProtocolError",
    "WearableVoiceSession",
    "acheck_credentials",
    "auth_headers",
    "bearer_query_param",
    "check_credentials",
    "describe_key",
    "load_client_env",
    "resolve_backend_url",
    "websocket_url",
]
