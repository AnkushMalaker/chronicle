"""Shared pytest fixtures and test environment defaults.

Several backend modules (notably ``advanced_omi_backend.auth``) validate that
required secrets are configured at *import* time. In CI there is no ``.env``
file, so importing the app during test collection would raise
``ValueError: <VAR> is not set``. We provide deterministic test defaults here so
collection succeeds without depending on a developer's local ``.env``.

``setdefault`` is used so a real environment (CI secrets or a local ``.env``
already exported) always wins over these placeholders.

It also provides ``redis_service`` / ``mongo_service``, which skip a module when
the backing service is not running — see those fixtures for why.
"""

import os
import socket
from urllib.parse import urlparse

import pytest

# Import-time required secrets (see advanced_omi_backend.auth).
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")


class _InMemorySystemEventQueue:
    """Minimal Redis-list surface that keeps test events inside the test process."""

    def __init__(self):
        self.items = []

    def lpush(self, key, value):
        self.items.insert(0, (key, value))

    def ltrim(self, _key, _start, max_index):
        del self.items[max_index + 1 :]


@pytest.fixture(autouse=True)
def isolated_system_event_ingest(monkeypatch):
    """Never let a unit test enqueue operational events into a live Redis."""

    # Fixture-local import lets tests reset module state without escaping this guard.
    from advanced_omi_backend.services.observability import system_events

    queue = _InMemorySystemEventQueue()
    monkeypatch.setattr(system_events, "_get_sync_redis", lambda: queue)
    return queue


# --- Tests that need a real backing service ---------------------------------
#
# A few modules genuinely exercise Redis or MongoDB rather than mocking them.
# Vault writes take a Redis lock that fails CLOSED by design (see
# services/memory/vault_lock.py — "Redis is down" must never mean "proceed
# unlocked"), and the audio-chunk and silence-trim tests assert against real
# Mongo documents. Neither can be faked without testing something other than
# what ships.
#
# Without these fixtures, a developer with no containers running gets 20
# failures and errors that look like broken code but are really just a missing
# service — each after a multi-second connection timeout. Skipping with the
# command to fix it keeps a bare checkout's test run honest and fast.
#
# Start both locally with the same ports CI uses:
#   podman run -d --rm --name chr-test-redis -p 6379:6379 redis:7-alpine
#   podman run -d --rm --name chr-test-mongo -p 27018:27017 mongo:8


def _reachable(url: str, default_port: int, timeout: float = 0.5) -> bool:
    """Whether something is listening where this URL points."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def redis_service():
    """Skip the module unless Redis is reachable."""
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    if not _reachable(url, 6379):
        pytest.skip(
            f"needs Redis at {url} — "
            "podman run -d --rm --name chr-test-redis -p 6379:6379 redis:7-alpine"
        )


@pytest.fixture(scope="session")
def mongo_service():
    """Skip the module unless MongoDB is reachable."""
    url = os.getenv("MONGODB_URI", "mongodb://localhost:27018")
    if not _reachable(url, 27017):
        pytest.skip(
            f"needs MongoDB at {url} — "
            "podman run -d --rm --name chr-test-mongo -p 27018:27017 mongo:8"
        )
