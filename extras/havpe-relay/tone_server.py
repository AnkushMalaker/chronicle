"""Local HTTP server for serving backend-generated audio to the HAVPE device.

The ESP32 media_player plays audio by fetching an HTTP URL, but the device is
LAN-only and can't reach the (possibly remote) backend. Protocol-v1 response WAVs
therefore go into a short-lived local staging directory served by this tiny on-LAN
HTTP server. The response binding remains owned by the relay.
"""

import itertools
import logging
import os
import socket
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# Runtime staging dir for backend-pushed audio (created on first use). Not bundled
# in the image or tracked in git — every file here is regenerable.
STAGING_DIR = Path(__file__).resolve().parent / "audio_cache"

# Backend audio is written into STAGING_DIR with a "_dyn_" prefix and served by the
# HTTP server. Unique names avoid device-side caching; old ones are pruned so the
# dir doesn't grow without bound.
_DYN_PREFIX = "_dyn_"
_DYN_KEEP = 3
_dyn_seq = itertools.count(1)

_DEFAULT_PORT = int(os.getenv("TONE_HTTP_PORT", "8990"))

_server: HTTPServer | None = None
_base_url: str | None = None
_lock = threading.Lock()


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler that logs at debug level instead of stderr."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("tone-http: %s", format % args)


def get_local_ip() -> str:
    """Best-effort LAN IP that the device can route back to."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def ensure_tone_server(port: int = _DEFAULT_PORT) -> str:
    """Start the tone HTTP server if not already running. Returns its base URL."""
    global _server, _base_url
    with _lock:
        if _server is not None and _base_url is not None:
            return _base_url
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        handler = partial(_QuietHandler, directory=str(STAGING_DIR))
        _server = HTTPServer(("0.0.0.0", port), handler)
        threading.Thread(
            target=_server.serve_forever, daemon=True, name="tone-http"
        ).start()
        _base_url = f"http://{get_local_ip()}:{port}"
        logger.info("Audio server started at %s (serving %s)", _base_url, STAGING_DIR)
        return _base_url


def _prune_dynamic() -> None:
    """Keep only the newest _DYN_KEEP dynamic audio files."""
    files = sorted(STAGING_DIR.glob(f"{_DYN_PREFIX}*"), key=lambda p: p.stat().st_mtime)
    for old in files[:-_DYN_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def serve_audio_bytes(data: bytes, ext: str = "wav", port: int = _DEFAULT_PORT) -> str:
    """Write audio bytes to a uniquely-named local file and return its served URL.

    Used for backend-generated audio (e.g. TTS) that the device must fetch on the
    LAN because it cannot reach the backend directly.
    """
    base = ensure_tone_server(port)
    name = f"{_DYN_PREFIX}{next(_dyn_seq)}.{ext}"
    (STAGING_DIR / name).write_bytes(data)
    _prune_dynamic()
    return f"{base}/{name}"
