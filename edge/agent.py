"""
Edge discovery agent — advertises Chronicle services on the Tailnet via minidisc.

Config-driven: reads what to advertise from environment variables instead of
probing ports. One agent per machine.

Two input modes (both contribute to a single service list):

  ADVERTISE          — comma-separated name:port pairs (edge nodes + overrides)
                       e.g. ADVERTISE=chronicle-speaker:8085,chronicle-asr:8767

  ADVERTISE_BACKEND  — when "true", always includes chronicle-backend:8000 and
                       inspects SPEAKER_SERVICE_URL / PARAKEET_ASR_URL /
                       OPENMEMORY_MCP_URL / TTS_SERVICE_URL to detect co-located
                       services (main server mode).
"""

import logging
import os
import signal
import socket
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [discovery-agent] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

shutdown = False

# Mapping from env-var URL → (service name, port)
_BACKEND_SERVICE_MAP = {
    "SPEAKER_SERVICE_URL": ("chronicle-speaker", 8085),
    "PARAKEET_ASR_URL": ("chronicle-asr", 8767),
    "OPENMEMORY_MCP_URL": ("chronicle-openmemory", 8765),
    "TTS_SERVICE_URL": ("chronicle-tts", 8770),
}


def _handle_signal(signum, frame):
    global shutdown
    logger.info("Received signal %s, shutting down", signal.Signals(signum).name)
    shutdown = True


def _collect_services() -> list[tuple[str, int]]:
    """Build the list of (name, port) pairs to advertise."""
    services: list[tuple[str, int]] = []
    seen: set[str] = set()

    def _add(name: str, port: int):
        if name not in seen:
            services.append((name, port))
            seen.add(name)

    # Mode 1: explicit ADVERTISE env var
    advertise = os.environ.get("ADVERTISE", "").strip()
    if advertise:
        for entry in advertise.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                logger.warning(
                    "Skipping invalid ADVERTISE entry (missing port): %s", entry
                )
                continue
            name, port_str = entry.rsplit(":", 1)
            try:
                _add(name.strip(), int(port_str.strip()))
            except ValueError:
                logger.warning("Skipping invalid ADVERTISE entry (bad port): %s", entry)

    # Mode 2: backend mode — read service URLs from .env
    if os.environ.get("ADVERTISE_BACKEND", "").lower() in ("true", "1", "yes"):
        _add("chronicle-backend", 8000)

        for env_var, (svc_name, svc_port) in _BACKEND_SERVICE_MAP.items():
            url = os.environ.get(env_var, "")
            if url and "host.docker.internal" in url:
                _add(svc_name, svc_port)

    return services


def main():
    global shutdown

    services = _collect_services()
    if not services:
        logger.error(
            "Nothing to advertise. Set ADVERTISE=name:port and/or ADVERTISE_BACKEND=true"
        )
        sys.exit(1)

    hostname = socket.gethostname()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        import minidisc
    except ImportError:
        logger.error("minidisc-python not installed")
        sys.exit(1)

    logger.info("Starting minidisc registry...")
    registry = minidisc.start_registry()

    labels = {"type": "discovery-agent", "host": hostname}
    for name, port in services:
        registry.advertise_service(port, name, labels)
        logger.info("Advertising %s on port %d", name, port)

    logger.info("Discovery agent running — %d service(s) advertised", len(services))

    while not shutdown:
        time.sleep(60)

    logger.info("Discovery agent stopped")


if __name__ == "__main__":
    main()
