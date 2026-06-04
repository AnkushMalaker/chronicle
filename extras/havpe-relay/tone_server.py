"""Local HTTP server for serving short notification tones to the HAVPE device.

The ESP32 media_player plays audio by fetching an HTTP URL, so the tones bundled
with the relay are served from a tiny on-LAN HTTP server. This keeps tone latency
local instead of round-tripping a URL through the (possibly remote) backend.

Tone assets live in ``tones/`` and are the Home Assistant Voice PE sounds
(CC-BY 4.0, see tones/LICENSE.md).
"""

import logging
import os
import socket
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)

TONES_DIR = Path(__file__).resolve().parent / "tones"

# Logical tone name (sent by the backend) -> filename in tones/
TONE_FILES: dict[str, str] = {
    "armed": "armed.flac",  # wake word triggered ("listening")
    "done": "done.wav",  # end of turn ("processing")
}

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
        handler = partial(_QuietHandler, directory=str(TONES_DIR))
        _server = HTTPServer(("0.0.0.0", port), handler)
        threading.Thread(
            target=_server.serve_forever, daemon=True, name="tone-http"
        ).start()
        _base_url = f"http://{get_local_ip()}:{port}"
        logger.info("Tone server started at %s (serving %s)", _base_url, TONES_DIR)
        return _base_url


def tone_url(tone: str, port: int = _DEFAULT_PORT) -> str | None:
    """Resolve a logical tone name to a locally-served URL, or None if unknown."""
    filename = TONE_FILES.get(tone)
    if not filename:
        logger.warning("Unknown tone '%s' (known: %s)", tone, list(TONE_FILES))
        return None
    if not (TONES_DIR / filename).exists():
        logger.warning("Tone file missing: %s", TONES_DIR / filename)
        return None
    return f"{ensure_tone_server(port)}/{filename}"
