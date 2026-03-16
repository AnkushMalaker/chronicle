"""
Edge discovery agent — advertises Chronicle services on the Tailnet via minidisc.

Config-driven: reads what to advertise from the ADVERTISE environment variable
instead of probing ports. One agent per machine.

  ADVERTISE  — comma-separated name:port pairs
               e.g. ADVERTISE=chronicle-backend:8000,chronicle-speaker:8085

When launched by services.py, the ADVERTISE string is built automatically from
configured services. Per-service compose files (e.g. extras/asr-services) set
their own ADVERTISE for distributed deployments.
"""

import logging
import os
import signal
import socket
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [discovery-agent] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

shutdown = False


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

    return services


def main():
    global shutdown

    services = _collect_services()
    if not services:
        logger.error("Nothing to advertise. Set ADVERTISE=name:port,...")
        sys.exit(1)

    hostname = socket.gethostname()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        import minidisc
    except ImportError:
        logger.error("minidisc-python not installed")
        sys.exit(1)

    def _try_start_registry():
        """start_registry() hangs if the background thread can't bind.
        Run it with a timeout so we can detect the failure."""
        holder = []

        def _go():
            holder.append(minidisc.start_registry())

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        t.join(timeout=10)
        return holder[0] if holder else None

    # Retry with backoff — on WSL2, the Tailscale interface may not be
    # visible inside Docker even with network_mode:host.  On real Linux
    # (RPi, bare-metal) it works immediately.
    registry = None
    backoff = [0, 10, 30, 60]
    for attempt, delay in enumerate(backoff):
        if shutdown:
            break
        if delay:
            logger.info(
                "Retrying in %ds (attempt %d/%d)...", delay, attempt + 1, len(backoff)
            )
            for _ in range(delay):
                if shutdown:
                    break
                time.sleep(1)
        logger.info("Starting minidisc registry...")
        registry = _try_start_registry()
        if registry:
            break
        logger.warning(
            "minidisc registry startup timed out (Tailscale interface not bindable)"
        )

    if not registry:
        logger.error(
            "Could not start minidisc registry after %d attempts. "
            "Tailscale interface may not be reachable from this container "
            "(common on WSL2/Docker Desktop). Exiting.",
            len(backoff),
        )
        sys.exit(1)

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
