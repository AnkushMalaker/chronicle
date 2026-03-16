"""
Zero-config service discovery for Chronicle on Tailscale networks.

Thin wrapper around minidisc-python. Services advertise themselves on the
Tailnet; consumers discover them automatically. Every function is safe to call
even when Tailscale or minidisc is unavailable — they return None gracefully.

Resolution priority (used by resolve_service_url):
  1. Explicit environment variable (manual override always wins)
  2. Minidisc discovery (automatic if Tailscale available)
  3. Default value / disabled
"""

import logging
import os
import stat
from typing import Optional

logger = logging.getLogger(__name__)

# ── Service name constants ──────────────────────────────────────────────
CHRONICLE_BACKEND = "chronicle-backend"
CHRONICLE_SPEAKER = "chronicle-speaker"
CHRONICLE_ASR = "chronicle-asr"
CHRONICLE_OPENMEMORY = "chronicle-openmemory"
CHRONICLE_LLM = "chronicle-llm"
CHRONICLE_TTS = "chronicle-tts"
CHRONICLE_RELAY = "chronicle-relay"

_TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"


def is_tailscale_available() -> bool:
    """Check whether the Tailscale daemon socket exists on this machine.

    Uses stat to verify the path is actually a Unix socket, not just a
    directory (Docker creates empty dirs for missing bind-mount sources).
    """
    try:
        return stat.S_ISSOCK(os.stat(_TAILSCALE_SOCKET).st_mode)
    except (OSError, ValueError):
        return False


def advertise_service(
    port: int,
    name: str,
    labels: Optional[dict] = None,
):
    """Advertise a service on the local Tailnet.

    Returns the registry handle (keep a reference to stay advertised),
    or None if Tailscale/minidisc is unavailable.
    """
    try:
        import threading

        import minidisc

        # start_registry() blocks on ready.wait() with no timeout.
        # If the server thread can't bind (e.g., Docker container without
        # Tailscale interfaces), it hangs forever. Run with a timeout.
        registry_holder = []

        def _try_start():
            registry_holder.append(minidisc.start_registry())

        t = threading.Thread(target=_try_start, daemon=True)
        t.start()
        t.join(timeout=5)

        if not registry_holder:
            logger.debug(
                "minidisc registry startup timed out after 5s — skipping advertisement"
            )
            return None

        registry = registry_holder[0]
        registry.advertise_service(port, name, labels or {})
        logger.info("Advertising '%s' on port %d via minidisc", name, port)
        return registry
    except ImportError:
        logger.debug("minidisc not installed — skipping service advertisement")
    except Exception as e:
        logger.debug("minidisc advertisement failed (non-fatal): %s", e)
    return None


def discover_service(
    name: str,
    labels: Optional[dict] = None,
    timeout: int = 3,
) -> Optional[str]:
    """Discover a service on the Tailnet by name.

    Returns ``"http://{addr}:{port}"`` or None if not found.
    """
    try:
        import minidisc

        endpoint = minidisc.find_service(name, labels or {})
        if endpoint:
            url = f"http://{endpoint}"
            logger.info("Discovered '%s' at %s", name, url)
            return url
    except ImportError:
        logger.debug("minidisc not installed — skipping service discovery")
    except Exception as e:
        logger.debug("minidisc discovery for '%s' failed (non-fatal): %s", name, e)
    return None


def resolve_service_url(
    env_var: Optional[str],
    service_name: str,
    labels: Optional[dict] = None,
    default: Optional[str] = None,
) -> Optional[str]:
    """Resolve a service URL with graceful fallback.

    Priority:
      1. Environment variable (if env_var is set and non-empty)
      2. Minidisc discovery on Tailnet
      3. default
    """
    if env_var:
        value = os.getenv(env_var)
        if value:
            return value

    discovered = discover_service(service_name, labels)
    if discovered:
        return discovered

    return default


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse a minidisc endpoint string like '100.99.62.5:8989' into (address, port)."""
    if not endpoint:
        return ("", 0)
    try:
        host, port_str = endpoint.rsplit(":", 1)
        return (host, int(port_str))
    except (ValueError, AttributeError):
        return (endpoint, 0)


def list_all_services() -> list[dict]:
    """List all chronicle-* services on the Tailnet via minidisc.

    Returns a list of dicts with keys: name, address, port, labels.
    Gracefully returns [] on ImportError or any exception.
    """
    try:
        import minidisc

        raw = minidisc.list_services()
        results = []
        for svc in raw:
            name = getattr(svc, "name", None) or (
                svc.get("name") if isinstance(svc, dict) else None
            )
            if not name or not name.startswith("chronicle-"):
                continue
            labels = (
                getattr(svc, "labels", {})
                if not isinstance(svc, dict)
                else svc.get("labels", {})
            )
            # minidisc Service objects use 'endpoint' (e.g. "100.x.x.x:8000"),
            # not separate address/port fields.
            endpoint = (
                getattr(svc, "endpoint", "")
                if not isinstance(svc, dict)
                else svc.get("endpoint", "")
            )
            address, port = _parse_endpoint(str(endpoint))
            results.append(
                {
                    "name": name,
                    "address": address,
                    "port": port,
                    "labels": labels,
                }
            )
        return results
    except ImportError:
        logger.debug("minidisc not installed — cannot list services")
    except Exception as e:
        logger.debug("minidisc list_services failed (non-fatal): %s", e)
    return []
