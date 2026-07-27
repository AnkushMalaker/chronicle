"""macOS menu bar app for the HAVPE TCP relay.

Runs the TCP-to-WebSocket relay in a background asyncio thread and shows
connection status in the macOS menu bar. No terminal needed.

Uses the shared relay_core module for Wyoming protocol forwarding.
"""

import asyncio
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import rumps
from dotenv import load_dotenv
from relay_core import RelayConfig, check_credentials, run_device_session

logger = logging.getLogger(__name__)

load_dotenv()

RELAY_PORT = int(os.getenv("RELAY_PORT", "8989"))


class MemoryLogHandler(logging.Handler):
    """Keep recent formatted log lines in memory for display in the menu bar UI."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = MemoryLogHandler()


def _show_logs_dialog(title: str, lines) -> None:
    """Show log lines in a scrollable modal dialog."""
    # Lazy import: macOS-only (AppKit/Foundation, not available cross-platform)
    from AppKit import (
        NSAlert,
        NSBezelBorder,
        NSFont,
        NSScrollView,
        NSTextView,
        NSViewWidthSizable,
    )
    from Foundation import NSMakeRect, NSMakeSize

    text = "\n".join(lines) or "(no logs yet)"
    width, height = 720, 380

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setBorderType_(NSBezelBorder)
    scroll.setAutohidesScrollers_(False)

    content = scroll.contentSize()
    text_view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, content.width, content.height)
    )
    text_view.setMinSize_(NSMakeSize(0, content.height))
    text_view.setMaxSize_(NSMakeSize(1e7, 1e7))
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    text_view.setAutoresizingMask_(NSViewWidthSizable)
    text_view.setEditable_(False)
    text_view.setFont_(NSFont.userFixedPitchFontOfSize_(11))
    text_view.textContainer().setContainerSize_(NSMakeSize(content.width, 1e7))
    text_view.textContainer().setWidthTracksTextView_(True)
    text_view.setString_(text)
    text_view.scrollRangeToVisible_((len(text), 0))

    scroll.setDocumentView_(text_view)

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(f"Last {len(lines)} log line(s)")
    alert.addButtonWithTitle_("Close")
    alert.setAccessoryView_(scroll)
    alert.runModal()


# --- Shared state ------------------------------------------------------------


@dataclass
class SharedState:
    """Thread-safe state shared between rumps UI and the asyncio relay thread."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    status: str = "idle"  # idle | listening | connected | error
    error: Optional[str] = None
    device_addr: Optional[str] = None
    chunks_sent: int = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "error": self.error,
                "device_addr": self.device_addr,
                "chunks_sent": self.chunks_sent,
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


# --- Asyncio background thread -----------------------------------------------


class AsyncioThread:
    """Runs an asyncio event loop in a daemon thread."""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        while self.loop is None or not self.loop.is_running():
            threading.Event().wait(0.01)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


# --- Relay manager ------------------------------------------------------------


class RelayManager:
    """Manages the TCP relay server lifecycle in the background asyncio thread."""

    def __init__(self, state: SharedState, bg: AsyncioThread) -> None:
        self.state = state
        self.bg = bg
        self.config = RelayConfig.from_env()
        self._server: Optional[asyncio.AbstractServer] = None
        self._session_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        self.bg.run_coro(self._start_server())

    def stop(self) -> None:
        self.bg.run_coro(self._stop_server())

    async def _start_server(self) -> None:
        if self._server is not None:
            return

        if not self.config.api_key:
            self.state.update(status="error", error="CHRONICLE_API_KEY not set in .env")
            return

        if not await check_credentials(self.config.api_key, self.config.backend_url):
            self.state.update(status="error", error="Backend auth failed")
            return

        state = self.state
        config = self.config

        def _make_handler(r, w):
            def on_chunk(_payload: bytes, _length: int) -> None:
                with state._lock:
                    state.chunks_sent += 1

            def on_session_start(addr_str: str) -> None:
                state.update(status="connected", device_addr=addr_str, chunks_sent=0)

            def on_session_end() -> None:
                state.update(status="listening", device_addr=None)

            def on_auth_failure() -> None:
                state.update(status="listening", device_addr=None, error="Auth failed")

            task = asyncio.ensure_future(
                run_device_session(
                    r,
                    w,
                    config,
                    on_audio_chunk=on_chunk,
                    on_session_start=on_session_start,
                    on_session_end=on_session_end,
                    on_auth_failure=on_auth_failure,
                )
            )
            self._session_tasks.add(task)
            task.add_done_callback(self._session_tasks.discard)

        self._server = await asyncio.start_server(_make_handler, "0.0.0.0", RELAY_PORT)
        self.state.update(status="listening", error=None)
        logger.info("Relay listening on :%d", RELAY_PORT)

    async def _stop_server(self) -> None:
        if self._server is None:
            return

        # Stop accepting new connections
        self._server.close()

        # Cancel active sessions first — their finally blocks close the TCP writers,
        # which is what allows wait_closed() to return.
        for task in list(self._session_tasks):
            task.cancel()
        if self._session_tasks:
            await asyncio.gather(*self._session_tasks, return_exceptions=True)
        self._session_tasks.clear()

        await self._server.wait_closed()
        self._server = None

        self.state.update(status="idle", device_addr=None, chunks_sent=0)
        logger.info("Relay stopped")


# --- rumps menu bar app -------------------------------------------------------


class RelayMenuApp(rumps.App):
    """macOS menu bar app for the HAVPE relay."""

    def __init__(self, state: SharedState, relay: RelayManager) -> None:
        super().__init__("Chronicle Relay", title="\u2299\u02b0\u1d43")
        self.state = state
        self.relay = relay

        self.status_item = rumps.MenuItem("Status: Starting...", callback=None)
        self.toggle_item = rumps.MenuItem("Stop Relay", callback=self.on_toggle)
        self.logs_item = rumps.MenuItem("View Logs", callback=self.on_view_logs)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            self.logs_item,
            None,
        ]

    @rumps.timer(2)
    def refresh_ui(self, _sender) -> None:
        snap = self.state.snapshot()
        status = snap["status"]

        if status == "connected":
            self.title = "\u25cf\u02b0\u1d43"  # filled circle + ha
            addr = snap["device_addr"] or "?"
            chunks = f"{snap['chunks_sent']:,}"
            self.status_item.title = (
                f"Status: Connected ({addr}) \u2014 {chunks} chunks"
            )
            self.toggle_item.title = "Stop Relay"
        elif status == "listening":
            self.title = "\u2299\u02b0\u1d43"  # circled dot + ha
            self.status_item.title = f"Status: Listening on :{RELAY_PORT}"
            self.toggle_item.title = "Stop Relay"
        elif status == "error":
            self.title = "\u2298\u02b0\u1d43"  # circled division slash + ha
            self.status_item.title = (
                f"Status: Error \u2014 {snap['error'] or 'unknown'}"
            )
            self.toggle_item.title = "Start Relay"
        else:
            self.title = "\u2298\u02b0\u1d43"  # stopped + ha
            self.status_item.title = "Status: Stopped"
            self.toggle_item.title = "Start Relay"

    def on_toggle(self, _sender) -> None:
        snap = self.state.snapshot()
        if snap["status"] in ("listening", "connected"):
            logger.info("User stopping relay")
            self.relay.stop()
        else:
            logger.info("User starting relay")
            self.relay.start()

    def on_view_logs(self, _sender) -> None:
        """Show recent log output in a scrollable dialog."""
        _show_logs_dialog("Chronicle Relay — Logs", list(log_buffer.lines))


# --- Entry point --------------------------------------------------------------


def main() -> None:
    # Lazy import: macOS-only (AppKit, not available cross-platform)
    from AppKit import NSApplication

    NSApplication.sharedApplication().setActivationPolicy_(1)

    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(format=log_format, level=logging.INFO)
    log_buffer.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(log_buffer)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)

    state = SharedState()
    bg = AsyncioThread()
    bg.start()

    relay = RelayManager(state, bg)
    relay.start()

    app = RelayMenuApp(state, relay)
    app.run()


if __name__ == "__main__":
    main()
