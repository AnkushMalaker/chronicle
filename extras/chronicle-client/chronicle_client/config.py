"""Where the backend is, and what credential to reach it with.

Every native client (tray, vault sync, wearable client, havpe relay) needs the
same four facts and used to derive them independently — four copies of the
repo-root ``.env`` load, four copies of the discovery fallback, four readings of
``CHRONICLE_API_KEY``. This is the single source.
"""

import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Files that only exist together at the repository root. Used as the marker
# rather than a fixed number of parent hops, because that only holds when the
# package is installed editable — a regular install puts this module in
# site-packages, where counting parents silently yields the wrong directory
# (and a silently-empty API key).
_ROOT_MARKERS = ("discovery.py", "setup_utils.py")


def _looks_like_repo_root(path: Path) -> bool:
    return all((path / marker).is_file() for marker in _ROOT_MARKERS)


def _find_repo_root() -> Path:
    """Locate the Chronicle checkout, or fall back to the source-tree layout."""
    override = os.getenv("CHRONICLE_REPO_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if _looks_like_repo_root(candidate):
            return candidate
        logger.warning("CHRONICLE_REPO_ROOT=%s does not look like a checkout", candidate)

    here = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    # `Path.parents` excludes the path itself, so include each start directory
    # explicitly — the common case is being run from the checkout root.
    for start in (here, cwd):
        for candidate in (start, *start.parents):
            if _looks_like_repo_root(candidate):
                return candidate

    # extras/chronicle-client/chronicle_client/config.py -> repo root
    return here.parents[3]


REPO_ROOT = _find_repo_root()

_env_loaded = False


def load_client_env() -> None:
    """Load the repository-root ``.env`` shared by all native client components.

    Idempotent: several sections of the tray call this during startup, and
    python-dotenv does not override already-set variables, so repeated calls are
    harmless — but skipping the re-read keeps startup logs clean.
    """
    global _env_loaded
    if _env_loaded:
        return
    load_dotenv(REPO_ROOT / ".env")
    _env_loaded = True


def resolve_backend_url(explicit: Optional[str] = None) -> str:
    """Resolve the backend base URL: explicit value > Tailnet discovery > localhost.

    ``discovery`` lives at the repository root and is not yet a package, so this
    is the one place in client code that has to put the repo root on ``sys.path``.
    Keeping it here means no other client module needs to know that.
    """
    if explicit is None:
        explicit = os.getenv("BACKEND_URL")

    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.append(root)
    try:
        from discovery import resolve_backend_url as _resolve
    except ImportError:
        logger.warning("discovery module unavailable; set BACKEND_URL in .env")
        return explicit or "http://localhost:8000"
    return _resolve(explicit, logger=logger)


def websocket_url(backend_url: str) -> str:
    """Derive the WebSocket base URL from an http(s) backend URL."""
    return backend_url.replace("https://", "wss://").replace("http://", "ws://")


@dataclass
class ClientConfig:
    """Connection settings shared by every native client."""

    backend_url: str
    backend_ws_url: str
    # Long-lived Chronicle API key (webui → Settings → API Keys). A JWT expires
    # after 24h and these clients have no way to log in again, so a key is the
    # only credential that survives a long-running session.
    api_key: str
    device_name: str
    verify_ssl: bool = True

    @classmethod
    def from_env(cls, *, default_device_name: Optional[str] = None) -> "ClientConfig":
        load_client_env()
        backend_url = resolve_backend_url()
        return cls(
            backend_url=backend_url,
            backend_ws_url=os.getenv("BACKEND_WS_URL") or websocket_url(backend_url),
            api_key=os.getenv("CHRONICLE_API_KEY", ""),
            device_name=(
                os.getenv("DEVICE_NAME")
                or default_device_name
                or socket.gethostname()
            ),
            verify_ssl=os.getenv("VERIFY_SSL", "true").lower() == "true",
        )
