"""Reading and masking values in ``.env`` files.

The setup path's lowest layer: every wizard and ``init.py`` needs to know what a
service is already configured with before it can offer to keep it.
"""

from pathlib import Path
from typing import Optional, Tuple

from dotenv import get_key


def read_env_value(env_file_path: str, key: str) -> Optional[str]:
    """
    Read a value from an .env file using python-dotenv.

    Args:
        env_file_path: Path to .env file
        key: Environment variable name

    Returns:
        Value if found, None otherwise

    Example:
        >>> value = read_env_value('.env', 'SMTP_HOST')
        >>> print(value)  # 'smtp.gmail.com' or None
    """
    env_path = Path(env_file_path)
    if not env_path.exists():
        return None

    value = get_key(str(env_path), key)
    # get_key returns None if key doesn't exist or value is empty
    return value if value else None


def resolve_ingest_config(
    search_paths: list,
    host: str = "host.docker.internal",
    default_port: str = "8000",
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the cross-service System-Errors ingest URL + token for a sidecar.

    Sidecar services (ASR, speaker-recognition) push their ERROR/CRITICAL logs to the
    backend's ``POST /api/admin/system-events/ingest`` so failures surface on the admin
    "System Errors" page instead of being buried in container logs. This sources the
    backend address + auth token the same way other shared secrets are sourced: from
    the backend .env (canonical hub on a main machine) or the repo-root .env (per-node
    store), in the given order.

    The token prefers a dedicated ``SYSTEM_EVENT_INGEST_TOKEN`` and falls back to
    ``SERVICE_MANAGER_TOKEN`` (which the backend accepts as the ingest fallback).

    Returns ``(ingest_url, ingest_token)``. Both are ``None`` when no backend config is
    found locally (e.g. a remote service node with no backend .env) — the reporter is
    opt-in and stays a no-op until both are set, so callers should only write non-None
    values and leave the keys untouched otherwise.

    Args:
        search_paths: .env paths to search, in priority order.
        host: Hostname the sidecar uses to reach the backend (default reaches the
            host gateway from inside a container).
        default_port: Backend HTTP port to use when ``BACKEND_PUBLIC_PORT`` is absent.

    Example:
        >>> url, token = resolve_ingest_config(["../../backends/advanced/.env", "../../.env"])
    """
    for path in search_paths:
        token = read_env_value(path, "SYSTEM_EVENT_INGEST_TOKEN") or read_env_value(
            path, "SERVICE_MANAGER_TOKEN"
        )
        if token:
            port = read_env_value(path, "BACKEND_PUBLIC_PORT") or default_port
            url = f"http://{host}:{port}/api/admin/system-events/ingest"
            return url, token
    return None, None


def is_placeholder(value: str, *placeholder_variants: str) -> bool:
    """
    Check if a value is a placeholder.

    Normalizes both the value and placeholders (treats hyphens/underscores as equivalent).

    Args:
        value: The value to check
        placeholder_variants: One or more placeholder strings to check against

    Returns:
        True if value matches any placeholder variant

    Example:
        >>> is_placeholder('your-key-here', 'your_key_here')
        True
        >>> is_placeholder('sk-abc123', 'your_key_here')
        False
    """
    if not value:
        return True

    # Normalize by replacing hyphens with underscores
    normalized_value = value.replace("-", "_").lower()

    for placeholder in placeholder_variants:
        normalized_placeholder = placeholder.replace("-", "_").lower()
        if normalized_value == normalized_placeholder:
            return True

    return False


def mask_value(value: str, show_chars: int = 5) -> str:
    """
    Mask a sensitive value, showing only first and last few characters.

    Args:
        value: The value to mask
        show_chars: Number of characters to show at start/end (default: 5)

    Returns:
        Masked string in format: "first5***********last5". Values too short to
        safely show partial characters (e.g. human-length passwords) are fully
        masked — every caller passes secrets, so leaking a short one verbatim
        is worse than showing nothing.

    Examples:
        >>> mask_value('sk-proj-abc123def456ghi789')
        'sk-pr***************i789'
        >>> mask_value('short')
        '*****'
        >>> mask_value('smtp_password_12345')
        'smtp_***********2345'
    """
    # Strip whitespace before processing
    value_clean = value.strip() if value else value

    if not value_clean:
        return value
    if len(value_clean) <= show_chars * 2:
        return "*" * len(value_clean)

    return f"{value_clean[:show_chars]}{'*' * min(15, len(value_clean) - show_chars * 2)}{value_clean[-show_chars:]}"
