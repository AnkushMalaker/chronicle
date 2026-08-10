"""Detection and attribution of event-loop stalls, in-process.

A single-threaded event loop that stops turning takes everything with it: health
checks time out, WebSocket reads stop draining, and every concurrent request waits
behind whatever is blocking. The process stays up and every container-level probe
keeps passing, so the failure is invisible from outside.

Diagnosing one previously took three manual steps — hand-timing an endpoint that
performs no I/O to establish that scheduling itself was delayed, then repeated
``py-spy dump`` samples to find the culprit (14/14 inside blocking ``redis-py``
socket reads). This module does that continuously.

Two mechanisms, answering two different questions:

**How long was the loop blocked?** A task sleeps for a fixed interval and measures
its own overshoot. A sleep scheduled for 250 ms that returns after 3 s means the
loop could not run it for 2.75 s. This costs four wakeups per second and is always
on.

**What blocked it?** A watchdog *thread* — not a task, because a blocked loop cannot
sample itself — reads the loop thread's stack via :func:`sys._current_frames` while
the stall is in progress. It samples repeatedly and reports the modal stack: one
sample is suggestive, twenty identical ones are a diagnosis. This works precisely in
the case that matters most, blocking socket I/O, because that releases the GIL. Its
blind spot is a C extension that blocks *while holding* the GIL — then the watchdog
cannot run either, and the stall is reported with its duration but no stack.

Both measurements are sampled, so both are accurate only to within one tick
(``TICK_SECONDS``). The lag reading *under*-reports — a block that begins partway
through the heartbeat's sleep only delays the part that remained. The watchdog's
duration *over*-reports by the same bound, since it measures from the last
successful tick rather than from the instant the block began. At 250 ms neither
matters for the multi-second stalls worth alarming on, but it does mean these
numbers are evidence of a stall, not a precise profile of one.

Relationship to :mod:`advanced_omi_backend.heartbeat`: that is a coarse cross-process
liveness signal (a consumer publishes a timestamp each main-loop pass; the container
healthcheck fails after 90 s of silence). It answers "is this worker wedged?". This
answers "is this loop being blocked, for how long, and by what?" — sub-second, and
long before anything looks unhealthy.

Configuration (all optional):

``EVENT_LOOP_MONITOR``           enable/disable entirely (default on)
``EVENT_LOOP_MONITOR_STACKS``    capture stacks during stalls (default on)
``EVENT_LOOP_STALL_SECONDS``     stall threshold, seconds (default 1.0)
``EVENT_LOOP_CRITICAL_SECONDS``  critical threshold, seconds (default 5.0)
``ASYNCIO_DEBUG``                asyncio's own slow-callback attribution (default off)
``ASYNCIO_SLOW_CALLBACK_SECONDS`` its threshold, seconds (default 0.25)

``ASYNCIO_DEBUG`` is the only one that costs anything. It names the exact callback
rather than a stack, but CPython gates that report behind full debug mode
(``BaseEventLoop._run_once`` only times handles ``if self._debug``), which also turns
on coroutine-origin tracking for every ``await``. It is a deliberate diagnosis-session
setting, not a steady-state one.
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional

from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.services.observability.system_events import record_event

logger = logging.getLogger("observability.loop_monitor")

# How often the heartbeat task measures its own scheduling delay. Every tick is a
# sample, so this also sets the resolution of the lag statistics.
TICK_SECONDS = 0.25

# A stall must exceed this to be captured and reported. It has to sit above
# TICK_SECONDS, since a healthy loop already runs the heartbeat up to one tick late.
DEFAULT_STALL_SECONDS = 1.0
DEFAULT_CRITICAL_SECONDS = 5.0

# While a stall is in progress the watchdog samples this often. Fast enough to
# collect a meaningful sample count from a one-second stall.
STACK_SAMPLE_SECONDS = 0.05
MAX_STACK_SAMPLES = 40
STACK_DEPTH = 14

# Lag samples retained for the statistics endpoint (~5 minutes at TICK_SECONDS).
WINDOW_SAMPLES = 1200
RECENT_STALLS = 10

# A recurring stall is one incident that accrues occurrences, not one row per
# occurrence. It is resolved after this long without a recurrence of that signature.
INCIDENT_RESOLVE_SECONDS = 900

# Published snapshot, so the API process can report stalls in the worker processes.
SNAPSHOT_KEY_PREFIX = "system:loopmon:"
SNAPSHOT_INTERVAL_SECONDS = 15
SNAPSHOT_TTL_SECONDS = 60


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Stall:
    """One period during which the loop did not turn."""

    started_at: float
    duration: float
    samples: int
    stack: tuple[str, ...]

    @property
    def signature(self) -> str:
        """The innermost frames, which is what identifies a recurring cause."""
        if not self.stack:
            return "no-stack"
        return " <- ".join(reversed(self.stack[-3:]))

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "duration_ms": round(self.duration * 1000, 1),
            "stack_samples": self.samples,
            "stack": list(self.stack),
        }


def _format_frames(frame) -> tuple[str, ...]:
    """Compact ``file:line in func`` lines, innermost last."""
    summary = traceback.extract_stack(frame)[-STACK_DEPTH:]
    return tuple(f"{f.filename}:{f.lineno} in {f.name}" for f in summary)


class LoopMonitor:
    """Measures scheduling delay on the running loop and attributes stalls."""

    def __init__(
        self,
        process: str,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        critical_seconds: float = DEFAULT_CRITICAL_SECONDS,
        capture_stacks: bool = True,
    ):
        self.process = process
        self.stall_seconds = stall_seconds
        self.critical_seconds = critical_seconds
        self.capture_stacks = capture_stacks

        self._lags: deque[float] = deque(maxlen=WINDOW_SAMPLES)
        self._recent: deque[Stall] = deque(maxlen=RECENT_STALLS)
        self._stall_count = 0
        self._started_at = time.time()

        # Written by the loop thread, read by the watchdog thread. A float
        # assignment is atomic under the GIL, so no lock is needed.
        self._last_tick = time.monotonic()
        self._loop_thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

        # Filled by the watchdog thread (and the asyncio log tap), drained by the
        # heartbeat task. Reporting an event means awaiting Mongo and Redis, which
        # must never happen on a non-loop thread — nor synchronously on the loop.
        self._pending: queue.SimpleQueue = queue.SimpleQueue()

        # signature -> last time it was seen, for incident open/resolve transitions.
        self._open_incidents: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Watchdog thread
    # ------------------------------------------------------------------ #

    def _watch(self) -> None:
        """Sample the loop thread's stack whenever it stops ticking."""
        poll = max(TICK_SECONDS, self.stall_seconds / 4)
        while not self._stop.is_set():
            if self._stop.wait(poll):
                return
            blocked_for = time.monotonic() - self._last_tick
            if blocked_for < self.stall_seconds:
                continue

            # A stall is in progress. Sample until the loop turns again.
            began = self._last_tick
            samples: list[tuple[str, ...]] = []
            while not self._stop.is_set() and self._last_tick == began:
                frames = sys._current_frames().get(self._loop_thread_id)
                if frames is not None:
                    try:
                        samples.append(_format_frames(frames))
                    except Exception:  # noqa: BLE001 — diagnostics must not raise
                        pass
                if len(samples) >= MAX_STACK_SAMPLES:
                    break
                self._stop.wait(STACK_SAMPLE_SECONDS)

            duration = time.monotonic() - began
            modal: tuple[str, ...] = ()
            if samples:
                modal = Counter(samples).most_common(1)[0][0]
            self._pending.put(
                Stall(
                    started_at=time.time() - duration,
                    duration=duration,
                    samples=len(samples),
                    stack=modal,
                )
            )

    # ------------------------------------------------------------------ #
    # Loop-side task
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Measure scheduling delay forever, reporting stalls as they end."""
        loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()

        if self.capture_stacks:
            self._watchdog = threading.Thread(
                target=self._watch, name=f"loop-monitor-{self.process}", daemon=True
            )
            self._watchdog.start()

        redis_client = None
        try:
            redis_client = create_async_redis()
        except Exception as e:  # noqa: BLE001 — stats publishing is best-effort
            logger.warning(f"Loop monitor will not publish snapshots: {e}")

        logger.info(
            f"⏱️  Event-loop monitor active for {self.process} "
            f"(stall>{self.stall_seconds}s, stacks={'on' if self.capture_stacks else 'off'})"
        )

        last_snapshot = 0.0
        try:
            while True:
                expected = loop.time() + TICK_SECONDS
                await asyncio.sleep(TICK_SECONDS)
                lag = max(0.0, loop.time() - expected)

                self._last_tick = time.monotonic()
                self._lags.append(lag)

                # A stall long enough to matter, where no watchdog is running to
                # attribute it, is still worth reporting on the lag measurement.
                if not self.capture_stacks and lag >= self.stall_seconds:
                    self._pending.put(
                        Stall(
                            started_at=time.time() - lag,
                            duration=lag,
                            samples=0,
                            stack=(),
                        )
                    )

                await self._drain_pending()
                await self._resolve_settled_incidents()

                now = time.monotonic()
                if redis_client is not None and (
                    now - last_snapshot >= SNAPSHOT_INTERVAL_SECONDS
                ):
                    last_snapshot = now
                    await self._publish(redis_client)
        except asyncio.CancelledError:
            raise
        finally:
            self._stop.set()
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _drain_pending(self) -> None:
        while True:
            try:
                stall = self._pending.get_nowait()
            except queue.Empty:
                return
            self._stall_count += 1
            self._recent.append(stall)
            await self._report(stall)

    async def _report(self, stall: Stall) -> None:
        where = stall.stack[-1] if stall.stack else "unattributed"
        logger.warning(
            f"Event loop stalled {stall.duration:.2f}s in {self.process} "
            f"({stall.samples} samples, innermost: {where})"
        )
        # One incident per cause, not per occurrence: the recorder collapses repeats
        # by incident key and increments occurrences until the cause is resolved.
        signature = stall.signature
        self._open_incidents[signature] = time.time()

        detail = (
            f"The {self.process} event loop did not run for {stall.duration:.2f}s. "
            f"Everything on it — health checks, WebSocket reads, in-flight requests — "
            f"waited that long.\n\n"
        )
        if stall.stack:
            detail += (
                f"Modal stack across {stall.samples} samples taken during the stall, "
                f"innermost last:\n" + "\n".join(stall.stack)
            )
        else:
            detail += (
                "No stack was captured. Either stack sampling is disabled, or the "
                "blocking code held the GIL throughout, which also prevents the "
                "watchdog thread from running."
            )

        await record_event(
            severity=(
                "critical" if stall.duration >= self.critical_seconds else "error"
            ),
            category="performance",
            source=__name__,
            title=(
                f"Event loop stalled {stall.duration:.1f}s in {self.process}"
                + (f": {stall.stack[-1].rsplit('/', 1)[-1]}" if stall.stack else "")
            ),
            detail=detail,
            metadata={
                "process": self.process,
                "duration_ms": round(stall.duration * 1000, 1),
                "stack_samples": stall.samples,
            },
            incident_key=f"event-loop-stall:{self.process}:{signature}",
        )

    async def _resolve_settled_incidents(self) -> None:
        """Close an incident once its cause has not recurred for a while."""
        now = time.time()
        for signature, last_seen in list(self._open_incidents.items()):
            if now - last_seen < INCIDENT_RESOLVE_SECONDS:
                continue
            del self._open_incidents[signature]
            await record_event(
                severity="info",
                category="performance",
                source=__name__,
                title=f"Event loop stalls cleared in {self.process}",
                detail=(
                    f"No recurrence for "
                    f"{INCIDENT_RESOLVE_SECONDS // 60} minutes: {signature}"
                ),
                incident_key=f"event-loop-stall:{self.process}:{signature}",
                resolves_incident=True,
            )

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        lags = sorted(self._lags)
        return {
            "process": self.process,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "samples": len(lags),
            "window_seconds": round(len(lags) * TICK_SECONDS, 1),
            "stacks_enabled": self.capture_stacks,
            "stall_threshold_ms": round(self.stall_seconds * 1000),
            "lag_p50_ms": _percentile(lags, 0.50),
            "lag_p95_ms": _percentile(lags, 0.95),
            "lag_p99_ms": _percentile(lags, 0.99),
            "lag_max_ms": _percentile(lags, 1.0),
            "stalls": self._stall_count,
            "recent_stalls": [s.as_dict() for s in reversed(self._recent)],
        }

    async def _publish(self, redis_client) -> None:
        try:
            await redis_client.set(
                f"{SNAPSHOT_KEY_PREFIX}{self.process}",
                json.dumps(self.stats()),
                ex=SNAPSHOT_TTL_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — never let diagnostics break a process
            logger.debug(f"Loop monitor snapshot not published: {e}")


def _percentile(sorted_lags: list[float], q: float) -> Optional[float]:
    if not sorted_lags:
        return None
    index = min(len(sorted_lags) - 1, int(round(q * (len(sorted_lags) - 1))))
    return round(sorted_lags[index] * 1000, 1)


# ---------------------------------------------------------------------- #
# Wiring
# ---------------------------------------------------------------------- #

_monitor: Optional[LoopMonitor] = None


def get_monitor() -> Optional[LoopMonitor]:
    """The monitor for this process, if one is running."""
    return _monitor


def apply_asyncio_debug() -> bool:
    """Turn on asyncio's own slow-callback attribution, if requested.

    This names the exact callback that ran long, which no stack sample can do as
    precisely. CPython only performs that timing in debug mode, which also enables
    coroutine-origin tracking on every ``await`` — so it is opt-in and temporary.
    """
    if not _env_flag("ASYNCIO_DEBUG", False):
        return False
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = _env_float("ASYNCIO_SLOW_CALLBACK_SECONDS", 0.25)
    logger.warning(
        f"asyncio debug mode ON (slow_callback_duration="
        f"{loop.slow_callback_duration}s). This is a diagnosis setting and carries "
        f"real overhead; asyncio logs offending callbacks at WARNING."
    )
    return True


def start_loop_monitor(process: str) -> Optional[asyncio.Task]:
    """Start the monitor on the running loop. Returns ``None`` when disabled."""
    global _monitor

    if not _env_flag("EVENT_LOOP_MONITOR", True):
        logger.info("Event-loop monitor disabled by EVENT_LOOP_MONITOR")
        return None

    apply_asyncio_debug()

    _monitor = LoopMonitor(
        process,
        stall_seconds=_env_float("EVENT_LOOP_STALL_SECONDS", DEFAULT_STALL_SECONDS),
        critical_seconds=_env_float(
            "EVENT_LOOP_CRITICAL_SECONDS", DEFAULT_CRITICAL_SECONDS
        ),
        capture_stacks=_env_flag("EVENT_LOOP_MONITOR_STACKS", True),
    )
    return asyncio.create_task(_monitor.run())
