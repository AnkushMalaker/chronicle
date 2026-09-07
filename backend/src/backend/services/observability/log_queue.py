"""Non-blocking log emission, so writing a log line cannot stall the event loop.

Emitting a record is synchronous I/O performed on whichever thread called
``logger.info()``. In the API process that thread is usually the event loop, and the
root logger's handlers both do real I/O:

* ``StreamHandler`` writes to stdout, which in a container is a pipe drained by the
  runtime. When the reader falls behind, ``write()`` blocks. A 1.15 s stall was
  recorded doing exactly this, from the request middleware's own log line.
* :class:`~backend.services.observability.log_handler.SystemEventLogHandler`
  pushes to Redis with the *synchronous* client, so any ``ERROR`` logged from the loop
  is a blocking round trip — and a blocking ``getaddrinfo`` when the connection has
  dropped.

Neither is fixable at the call sites: logging is meant to be callable from anywhere,
and the whole point of a log line is that you do not think about it. So the handlers
move instead. ``QueueHandler`` reduces the call site to an in-memory enqueue and a
single listener thread performs the real emission, which is the stdlib's own answer
for latency-sensitive callers. It costs one thread and changes no log output.

Deliberately scoped to the FastAPI process. An RQ work-horse is forked, and threads do
not survive ``fork()``, so a listener installed before the fork would leave the child
enqueueing into a queue nobody drains — silently losing its logs. Workers block on
their own logging instead, which is harmless: they have no event loop serving requests.
"""

import atexit
import logging
import queue
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

logger = logging.getLogger("observability.log_queue")

_listener: Optional[QueueListener] = None
_installed = False


def install_non_blocking_logging(target: Optional[logging.Logger] = None) -> None:
    """Move a logger's handlers behind a queue. Idempotent; defaults to the root.

    Call once, *after* every handler has been attached — anything added later keeps
    emitting on the caller's thread. ``target`` exists so this can be exercised
    against an isolated logger.
    """
    global _listener, _installed
    if _installed:
        return

    root = target if target is not None else logging.getLogger()
    handlers = [h for h in root.handlers if not isinstance(h, QueueHandler)]
    if not handlers:
        # Nothing to protect yet; installing now would silently swallow the handlers
        # attached afterwards, so leave logging alone and say so.
        logger.warning("Non-blocking logging not installed: root has no handlers")
        return

    # Unbounded, per the stdlib's own guidance: ``put_nowait`` must never block, which
    # is the entire point. A permanently stuck sink therefore grows memory rather than
    # stalling the loop — the better failure, and one the listener thread absorbs
    # first. SimpleQueue's put is implemented without a lock in CPython.
    record_queue: queue.SimpleQueue = queue.SimpleQueue()

    for handler in handlers:
        root.removeHandler(handler)
    root.addHandler(QueueHandler(record_queue))

    # respect_handler_level keeps each handler's own threshold meaningful; without it
    # the system-event handler would be asked to consider every INFO record.
    _listener = QueueListener(record_queue, *handlers, respect_handler_level=True)
    _listener.start()
    atexit.register(stop_non_blocking_logging)
    _installed = True
    logger.info(
        f"🧵 Logging moved off the event loop: {len(handlers)} handler(s) "
        f"now emit on a listener thread"
    )


def stop_non_blocking_logging() -> None:
    """Drain and stop the listener, so buffered records are not lost at exit."""
    global _listener, _installed
    if _listener is None:
        return
    try:
        _listener.stop()
    except Exception:  # noqa: BLE001 — shutdown must never raise
        pass
    _listener = None
    _installed = False
