"""In-memory log tail behind the tray's View Logs dialog.

systemd and launchd already capture the tray's stderr, but someone staring at
a stuck sync shouldn't have to reach for journalctl — the tray keeps its own
recent lines so it can show them in place.
"""

import logging
from collections import deque

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class MemoryLogHandler(logging.Handler):
    """Keep recent application log lines for display in the tray."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = MemoryLogHandler()


def configure_logging() -> None:
    """Log to stderr and the in-memory buffer, without adding the buffer twice."""
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    log_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    # basicConfig ignores level= when the root logger already has handlers.
    root.setLevel(logging.INFO)
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)
    logging.getLogger("httpx").setLevel(logging.WARNING)
