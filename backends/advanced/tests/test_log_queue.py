"""Logging must not block the thread that called it.

Emitting a record is synchronous I/O on the caller's thread. In the API process that
is the event loop, and both root handlers do real I/O — a stdout pipe write and a
synchronous Redis push. A 1.15 s loop stall was recorded inside ``StreamHandler.emit``,
reached from the request middleware's own log line.

What matters is therefore not that records still arrive, but that ``logger.info()``
*returns* while the handler is still working.

These run against an isolated, non-propagating logger rather than the root: pytest's
own logging plugin attaches a capture handler to the root around every test, so a test
written against it would be measuring pytest's handlers as much as ours.
"""

import logging
import logging.handlers
import threading
import time

import pytest

from advanced_omi_backend.services.observability import log_queue

pytestmark = pytest.mark.unit


class SlowHandler(logging.Handler):
    """Stands in for a blocked stdout pipe or an unreachable Redis."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay
        self.records: list[str] = []
        self.emitted = threading.Event()

    def emit(self, record):
        time.sleep(self.delay)
        self.records.append(record.getMessage())
        self.emitted.set()


@pytest.fixture
def isolated():
    """A logger of our own, detached from the root and torn down after each test."""
    target = logging.getLogger("test.log_queue.isolated")
    target.handlers = []
    target.propagate = False
    target.setLevel(logging.INFO)
    yield target
    log_queue.stop_non_blocking_logging()
    target.handlers = []


def test_a_slow_handler_no_longer_blocks_the_caller(isolated):
    """The whole point: the stall moves to the listener thread."""
    slow = SlowHandler(delay=0.5)
    isolated.addHandler(slow)

    log_queue.install_non_blocking_logging(isolated)

    started = time.monotonic()
    isolated.info("a line")
    elapsed = time.monotonic() - started

    # Would have been >= 0.5s emitting inline.
    assert elapsed < 0.1, f"logging blocked the caller for {elapsed:.3f}s"
    # ...and the record still gets there, just not on this thread.
    assert slow.emitted.wait(timeout=5)
    assert "a line" in slow.records


def test_records_survive_the_handover(isolated):
    """Moving handlers behind a queue must not drop or reorder anything."""
    slow = SlowHandler(delay=0)
    isolated.addHandler(slow)

    log_queue.install_non_blocking_logging(isolated)
    for i in range(50):
        isolated.info("line %d", i)
    log_queue.stop_non_blocking_logging()  # drains

    assert slow.records == [f"line {i}" for i in range(50)]


def test_installing_twice_does_not_stack_queues(isolated):
    isolated.addHandler(SlowHandler(delay=0))

    log_queue.install_non_blocking_logging(isolated)
    log_queue.install_non_blocking_logging(isolated)

    queue_handlers = [
        h for h in isolated.handlers if isinstance(h, logging.handlers.QueueHandler)
    ]
    assert len(queue_handlers) == 1


def test_it_declines_rather_than_swallowing_a_bare_logger(isolated):
    """Installing against no handlers would silently eat every one added later."""
    log_queue.install_non_blocking_logging(isolated)

    assert isolated.handlers == []

    # A handler attached afterwards must still receive records directly.
    slow = SlowHandler(delay=0)
    isolated.addHandler(slow)
    isolated.info("still delivered")
    assert slow.records == ["still delivered"]


def test_a_handler_below_its_own_level_is_not_asked_to_emit(isolated):
    """respect_handler_level: the system-event handler only wants ERROR and above."""
    errors_only = SlowHandler(delay=0)
    errors_only.setLevel(logging.ERROR)
    isolated.addHandler(errors_only)

    log_queue.install_non_blocking_logging(isolated)
    isolated.info("routine")
    isolated.error("broken")
    log_queue.stop_non_blocking_logging()

    assert errors_only.records == ["broken"]
