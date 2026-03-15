"""
Edge agent sidecar — advertises a Chronicle service on the Tailnet via minidisc.

Runs as a Docker container alongside the service it advertises.
Env vars:
  EDGE_SERVICE_NAME  — minidisc service name (e.g. chronicle-speaker)
  EDGE_SERVICE_PORT  — port to advertise (e.g. 8085)
"""

import logging
import os
import signal
import socket
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [edge-agent] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

shutdown = False


def _handle_signal(signum, frame):
    global shutdown
    logger.info("Received signal %s, shutting down", signal.Signals(signum).name)
    shutdown = True


def main():
    global shutdown

    service_name = os.environ.get("EDGE_SERVICE_NAME")
    service_port = os.environ.get("EDGE_SERVICE_PORT")

    if not service_name or not service_port:
        logger.error("EDGE_SERVICE_NAME and EDGE_SERVICE_PORT must be set")
        sys.exit(1)

    port = int(service_port)
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

    labels = {"type": "edge", "host": hostname}
    registry.advertise_service(port, service_name, labels)
    logger.info("Advertising %s on port %d (labels: %s)", service_name, port, labels)

    while not shutdown:
        time.sleep(60)

    logger.info("Edge agent stopped")


if __name__ == "__main__":
    main()
