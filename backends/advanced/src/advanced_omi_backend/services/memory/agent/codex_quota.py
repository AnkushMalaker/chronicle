"""Read the ChatGPT subscription's Codex quota, so background runs can yield to it.

Codex's rate limit is an account-wide weekly budget shared with the user's own
interactive sessions. Chronicle's background vault recording is the lower-priority
consumer of it: a memory extraction that is skipped still lands via the direct
(metered API) executor, whereas an interactive session that hits the wall is simply
blocked for days. So the agent checks headroom before spawning ``codex exec``.

The numbers come from the CLI itself rather than from parsing its error text: the
``codex app-server`` JSON-RPC surface exposes ``account/rateLimits/read``, which
returns per-bucket ``usedPercent`` / ``resetsAt`` / ``windowDurationMins``. That is
the same source the TUI's own usage display reads.
"""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("memory_service.agent.codex.quota")

# The app-server is spawned per read and killed as soon as the response arrives; the
# cache keeps that off the hot path when conversations close in quick succession.
_CACHE_TTL_SECONDS = 120
_READ_TIMEOUT_SECONDS = 20

_cache_lock = threading.Lock()
_cached: Optional[tuple[float, Optional[dict]]] = None


def read_rate_limits(
    *, timeout: int = _READ_TIMEOUT_SECONDS, use_cache: bool = True
) -> Optional[dict]:
    """Return the ``account/rateLimits/read`` payload, or ``None`` if unavailable.

    ``None`` means "could not determine" (no binary, no auth, timeout, protocol
    change) and is deliberately distinct from a snapshot reporting 100% used.
    Callers must not treat it as exhausted — see :func:`bucket_used_percent`.
    """
    global _cached
    if use_cache:
        with _cache_lock:
            if _cached and (time.monotonic() - _cached[0]) < _CACHE_TTL_SECONDS:
                return _cached[1]

    payload = _read_uncached(timeout)
    with _cache_lock:
        _cached = (time.monotonic(), payload)
    return payload


def _read_uncached(timeout: int) -> Optional[dict]:
    binary = shutil.which(os.environ.get("CODEX_BINARY", "codex"))
    if not binary:
        return None

    proc = None
    try:
        proc = subprocess.Popen(
            [binary, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        requests = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "chronicle-memory",
                            "title": "Chronicle memory agent",
                            "version": "1",
                        }
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {},
                }
            )
            + "\n"
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(requests)
        proc.stdin.flush()

        # The stream interleaves unsolicited notifications with responses, so read
        # until the id=2 reply appears or the deadline passes.
        deadline = time.monotonic() + timeout
        result: Optional[dict] = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                result = message.get("result")
                break
        if result is None:
            logger.debug("codex app-server returned no rate-limit response in time")
        return result
    except Exception as e:  # noqa: BLE001 — a quota probe must never break recording
        logger.debug("could not read codex rate limits (%s)", e)
        return None
    finally:
        if proc is not None:
            proc.kill()
            # Best-effort reap; the kill above is what actually ends the server.
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)


def bucket_used_percent(payload: Optional[dict], limit_id: str = "") -> Optional[int]:
    """Percent of the weekly Codex budget already spent, or ``None`` if unknown.

    ``limit_id`` selects a specific bucket from ``rateLimitsByLimitId`` (models are
    metered against different buckets — e.g. ``codex`` vs ``codex_bengalfox`` for
    Spark — and the account may have one exhausted while another is untouched).
    Empty selects the payload's own backward-compatible single-bucket view.
    """
    if not isinstance(payload, dict):
        return None
    snapshot = None
    if limit_id:
        by_id = payload.get("rateLimitsByLimitId")
        if isinstance(by_id, dict):
            snapshot = by_id.get(limit_id)
        if snapshot is None:
            # An unknown limit_id must not silently fall back to a different
            # bucket's headroom — that would gate on the wrong budget.
            logger.warning("codex rate-limit bucket %r not in payload", limit_id)
            return None
    else:
        snapshot = payload.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    primary = snapshot.get("primary")
    if not isinstance(primary, dict):
        return None
    used = primary.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return int(used)


def quota_span_attributes(
    payload: Optional[dict], limit_id: str = ""
) -> Dict[str, object]:
    """Flatten a snapshot into span attributes (empty when nothing is known)."""
    used = bucket_used_percent(payload, limit_id)
    if used is None:
        return {}
    attributes: Dict[str, object] = {"chronicle.memory.quota.used_percent": used}
    snapshot = (
        (payload or {}).get("rateLimitsByLimitId", {}).get(limit_id)
        if limit_id
        else (payload or {}).get("rateLimits")
    )
    if isinstance(snapshot, dict):
        primary = snapshot.get("primary")
        if isinstance(primary, dict):
            for source, target in (
                ("resetsAt", "chronicle.memory.quota.resets_at"),
                ("windowDurationMins", "chronicle.memory.quota.window_minutes"),
            ):
                value = primary.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    attributes[target] = value
        if snapshot.get("limitId"):
            attributes["chronicle.memory.quota.limit_id"] = snapshot["limitId"]
    return attributes
