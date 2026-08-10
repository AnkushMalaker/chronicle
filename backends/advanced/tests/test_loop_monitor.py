"""Tests for event-loop stall detection.

The failure being made visible: a synchronous call on the event loop — a blocking
socket read, a large serialization — stops every other task in the process. Nothing
crashes, no probe fails, and the only symptom is that everything is slow at once.
Diagnosing one by hand took timing an I/O-free endpoint to prove scheduling was
delayed, then repeated ``py-spy`` samples to find the cause.

Two properties matter and are tested separately, because they come from two
different mechanisms:

* the *measurement* — a stall is detected and its duration is roughly right;
* the *attribution* — the stack captured names the function that blocked.
"""

import asyncio
import time

import pytest

from advanced_omi_backend.services.observability import loop_monitor
from advanced_omi_backend.services.observability.loop_monitor import (
    LoopMonitor,
    Stall,
    _percentile,
    start_loop_monitor,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Never touch Mongo or Redis; capture what would have been recorded."""
    recorded: list[dict] = []

    async def _record(**kwargs):
        recorded.append(kwargs)

    def _no_redis():
        raise RuntimeError("no redis in tests")

    monkeypatch.setattr(loop_monitor, "record_event", _record)
    monkeypatch.setattr(loop_monitor, "create_async_redis", _no_redis)
    return recorded


def _blocking_call(seconds: float) -> None:
    """A synchronous call of the kind that must never run on the loop."""
    time.sleep(seconds)


async def _run_briefly(monitor: LoopMonitor, during) -> None:
    """Run the monitor, do something on the loop, then stop it."""
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.3)  # let the watchdog start and take a baseline
    during()
    await asyncio.sleep(0.6)  # let the stall be observed and drained
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


async def test_a_blocking_call_on_the_loop_is_detected(no_side_effects):
    monitor = LoopMonitor("test", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    assert monitor.stats()["stalls"] == 1
    stall = monitor.stats()["recent_stalls"][0]
    # Measured, not assumed: the reported duration reflects the real block.
    assert 700 <= stall["duration_ms"] <= 1500


async def test_an_idle_loop_reports_no_stalls(no_side_effects):
    monitor = LoopMonitor("test", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: None)

    stats = monitor.stats()
    assert stats["stalls"] == 0
    assert stats["recent_stalls"] == []
    # A loop with nothing to do schedules its own sleep on time.
    assert stats["lag_p50_ms"] < 50


async def test_a_block_under_the_threshold_is_measured_but_not_reported(
    no_side_effects,
):
    """Short blips belong in the statistics, not in the error feed."""
    monitor = LoopMonitor("test", stall_seconds=1.0)

    await _run_briefly(monitor, lambda: _blocking_call(0.4))

    assert monitor.stats()["stalls"] == 0
    assert no_side_effects == []
    # Sampled, so a 400ms block shows as at least 400ms minus one tick interval:
    # the sleep was already partway through when the block began. See TICK_SECONDS.
    assert monitor.stats()["lag_max_ms"] >= (0.4 - loop_monitor.TICK_SECONDS) * 1000


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


async def test_the_captured_stack_names_the_blocking_function(no_side_effects):
    """The question py-spy had to answer by hand: what was it executing?"""
    monitor = LoopMonitor("test", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    stack = monitor.stats()["recent_stalls"][0]["stack"]
    assert any("_blocking_call" in frame for frame in stack)
    # Sampled repeatedly rather than once, so the modal frame is evidence.
    assert monitor.stats()["recent_stalls"][0]["stack_samples"] > 1


async def test_a_stall_is_reported_as_an_incident_keyed_on_its_cause(no_side_effects):
    monitor = LoopMonitor("backend", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    assert len(no_side_effects) == 1
    event = no_side_effects[0]
    assert event["severity"] == "error"
    assert event["category"] == "performance"
    assert event["incident_key"].startswith("event-loop-stall:backend:")
    assert "_blocking_call" in event["detail"]


async def test_repeated_stalls_from_one_cause_share_an_incident_key(no_side_effects):
    """Otherwise a flapping loop files a fresh error row every second."""
    monitor = LoopMonitor("backend", stall_seconds=0.3)

    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.3)
    for _ in range(2):
        _blocking_call(0.5)
        await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(no_side_effects) == 2
    assert no_side_effects[0]["incident_key"] == no_side_effects[1]["incident_key"]


async def test_a_long_stall_is_critical(no_side_effects):
    monitor = LoopMonitor("test", stall_seconds=0.3, critical_seconds=0.5)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    assert no_side_effects[0]["severity"] == "critical"


async def test_stalls_are_still_measured_without_stack_capture(no_side_effects):
    """With the watchdog off, duration is still reported — just unattributed."""
    monitor = LoopMonitor("test", stall_seconds=0.3, capture_stacks=False)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    assert monitor.stats()["stalls"] == 1
    assert monitor.stats()["recent_stalls"][0]["stack"] == []
    assert "No stack was captured" in no_side_effects[0]["detail"]


# --------------------------------------------------------------------------- #
# Incident lifecycle
# --------------------------------------------------------------------------- #


async def test_an_incident_resolves_once_its_cause_stops_recurring(
    no_side_effects, monkeypatch
):
    monkeypatch.setattr(loop_monitor, "INCIDENT_RESOLVE_SECONDS", 0)
    monitor = LoopMonitor("backend", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    resolutions = [e for e in no_side_effects if e.get("resolves_incident")]
    assert len(resolutions) >= 1
    assert resolutions[0]["severity"] == "info"
    assert resolutions[0]["incident_key"] == no_side_effects[0]["incident_key"]


async def test_a_still_recurring_cause_is_not_resolved(no_side_effects):
    monitor = LoopMonitor("backend", stall_seconds=0.3)

    await _run_briefly(monitor, lambda: _blocking_call(0.8))

    assert not any(e.get("resolves_incident") for e in no_side_effects)


# --------------------------------------------------------------------------- #
# Statistics and configuration
# --------------------------------------------------------------------------- #


def test_percentiles_of_an_empty_window_are_unknown_not_zero():
    """Zero would read as a perfectly healthy loop that was never measured."""
    assert _percentile([], 0.5) is None


def test_percentiles_are_reported_in_milliseconds():
    assert _percentile([0.001, 0.002, 0.300], 1.0) == 300.0
    assert _percentile([0.001, 0.002, 0.300], 0.5) == 2.0


def test_a_stall_signature_is_its_innermost_frames():
    """Grouping on the leaf is what collapses one cause into one incident.

    The same blocking call reached from two different callers is one problem, so
    only the innermost frames take part in the signature.
    """
    inner = ("b.py:2 in middle", "c.py:3 in inner", "d.py:4 in blocking_read")
    one_caller = ("a.py:1 in handler_a",) + inner
    another_caller = ("z.py:9 in handler_z",) + inner

    assert (
        Stall(0, 1.0, 3, one_caller).signature
        == Stall(0, 2.0, 5, another_caller).signature
    )


def test_different_causes_do_not_share_a_signature():
    """...but two genuinely different blocking calls must stay separate incidents."""
    redis_read = ("a.py:1 in handler", "redis.py:9 in _read_from_socket")
    json_dump = ("a.py:1 in handler", "json.py:5 in dumps")

    assert (
        Stall(0, 1.0, 3, redis_read).signature != Stall(0, 1.0, 3, json_dump).signature
    )


def test_an_unattributed_stall_still_has_a_stable_signature():
    assert Stall(0, 1.0, 0, ()).signature == "no-stack"


async def test_the_monitor_can_be_disabled(monkeypatch, no_side_effects):
    monkeypatch.setenv("EVENT_LOOP_MONITOR", "false")

    assert start_loop_monitor("test") is None


async def test_thresholds_come_from_the_environment(monkeypatch, no_side_effects):
    monkeypatch.setenv("EVENT_LOOP_STALL_SECONDS", "2.5")
    monkeypatch.setenv("EVENT_LOOP_MONITOR_STACKS", "false")

    task = start_loop_monitor("test")
    try:
        monitor = loop_monitor.get_monitor()
        assert monitor.stall_seconds == 2.5
        assert monitor.capture_stacks is False
    finally:
        task.cancel()


async def test_a_malformed_threshold_falls_back_to_the_default(
    monkeypatch, no_side_effects
):
    """A typo in an env var must not disable stall detection silently."""
    monkeypatch.setenv("EVENT_LOOP_STALL_SECONDS", "one second")

    task = start_loop_monitor("test")
    try:
        assert loop_monitor.get_monitor().stall_seconds == (
            loop_monitor.DEFAULT_STALL_SECONDS
        )
    finally:
        task.cancel()
