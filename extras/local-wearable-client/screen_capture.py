"""macOS screen + accessibility capture (stage 1).

Captures a screenshot of every active display once per second and, for each
tick, reads the currently focused window's app/title via the Accessibility API.
Frames are written as JPEGs to a local folder (one per screen, suffixed `_i`
where `i` is the display index — `_0` is the main display) and the
focused-window info is logged.

Everything here is pure PyObjC — no Swift. It uses:
  - ScreenCaptureKit (`SCScreenshotManager`) for the screenshot, falling back to
    the deprecated Quartz `CGDisplayCreateImage` on macOS < 14
  - AppKit `NSBitmapImageRep` for JPEG encoding
  - `NSWorkspace` + the Accessibility API (`AXUIElement*`) for focused-window info

Two macOS permissions are required (TCC), both attached to the *host binary*:
  - Screen Recording  -> `CGRequestScreenCaptureAccess()`
  - Accessibility     -> `AXIsProcessTrustedWithOptions({prompt: True})`

When run from `uv`/python directly the grants attach to the interpreter (fine
for testing). For daily use, bundle this into a signed `.app` so the grants
stick to your app's bundle id instead.

Standalone test:
    uv run python screen_capture.py            # 1 fps, prints focused window, Ctrl-C to stop
    uv run python screen_capture.py --seconds 10
"""

import argparse
import datetime as _dt
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import objc
from AppKit import NSBitmapImageRep, NSImageCompressionFactor, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    kAXErrorSuccess,
)
from Quartz import (
    CGDisplayCopyDisplayMode,
    CGDisplayCreateImage,
    CGDisplayModeGetPixelHeight,
    CGDisplayModeGetPixelWidth,
    CGGetActiveDisplayList,
    CGImageGetHeight,
    CGImageGetWidth,
    CGMainDisplayID,
    CGPreflightScreenCaptureAccess,
    CGRequestScreenCaptureAccess,
)

# ScreenCaptureKit (macOS 12.3+, SCScreenshotManager needs 14.0+) is the modern
# replacement for the deprecated CGDisplayCreateImage. Import is optional so the
# module still loads (and falls back to CoreGraphics) on older systems.
try:
    from ScreenCaptureKit import (
        SCContentFilter,
        SCScreenshotManager,
        SCShareableContent,
        SCStreamConfiguration,
    )

    _SCK_OK = True
except Exception:  # pragma: no cover - depends on macOS version
    SCContentFilter = SCScreenshotManager = SCShareableContent = (
        SCStreamConfiguration
    ) = None
    _SCK_OK = False

logger = logging.getLogger(__name__)

# JPEG file-type constant moved/renamed across pyobjc versions.
try:
    from AppKit import NSBitmapImageFileTypeJPEG as _JPEG_TYPE
except ImportError:  # older pyobjc
    from AppKit import NSJPEGFileType as _JPEG_TYPE

# AX attribute / option constants. Use string literals so we don't depend on a
# particular pyobjc version re-exporting them (they're plain CFStrings anyway).
_kAXFocusedWindowAttribute = "AXFocusedWindow"
_kAXFocusedUIElementAttribute = "AXFocusedUIElement"
_kAXTitleAttribute = "AXTitle"
_kAXRoleAttribute = "AXRole"
_kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"

DEFAULT_CAPTURE_DIR = Path(
    os.environ.get("CAPTURE_DIR", Path.home() / "ChronicleCaptures")
)


# --- Permissions -------------------------------------------------------------


def screen_recording_ok(prompt: bool = False) -> bool:
    """Return True if Screen Recording is granted. If ``prompt`` and not yet
    granted, trigger the system TCC prompt (only effective once per process)."""
    if CGPreflightScreenCaptureAccess():
        return True
    if prompt:
        # Returns immediately; the grant takes effect after the user approves
        # and (for screen recording) usually after an app restart.
        return bool(CGRequestScreenCaptureAccess())
    return False


def accessibility_ok(prompt: bool = False) -> bool:
    """Return True if Accessibility (AX) is granted for this process. If
    ``prompt`` and not yet trusted, open the system prompt directing the user
    to System Settings -> Privacy & Security -> Accessibility."""
    if not prompt:
        return bool(AXIsProcessTrusted())
    options = {_kAXTrustedCheckOptionPrompt: True}
    return bool(AXIsProcessTrustedWithOptions(options))


def request_permissions() -> dict:
    """Trigger both TCC prompts and return current grant status."""
    rec = screen_recording_ok(prompt=True)
    acc = accessibility_ok(prompt=True)
    logger.info("Permissions — screen_recording=%s accessibility=%s", rec, acc)
    return {"screen_recording": rec, "accessibility": acc}


# --- Accessibility: focused window read --------------------------------------


def _ax_copy(element, attribute: str):
    """Wrapper over AXUIElementCopyAttributeValue returning the value or None."""
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if err != kAXErrorSuccess:
        return None
    return value


def read_focused_window() -> dict:
    """Read the frontmost app + its focused window title via the AX API.

    Returns a dict with: app, bundle_id, pid, window_title, focused_role.
    Missing pieces come back as None (e.g. if AX isn't granted, titles are None
    but the app name still resolves via NSWorkspace).
    """
    info = {
        "app": None,
        "bundle_id": None,
        "pid": None,
        "window_title": None,
        "focused_role": None,
    }

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return info

    info["app"] = front.localizedName()
    info["bundle_id"] = front.bundleIdentifier()
    pid = front.processIdentifier()
    info["pid"] = int(pid)

    app_el = AXUIElementCreateApplication(pid)
    if app_el is None:
        return info

    window = _ax_copy(app_el, _kAXFocusedWindowAttribute)
    if window is not None:
        info["window_title"] = _ax_copy(window, _kAXTitleAttribute)

    focused = _ax_copy(app_el, _kAXFocusedUIElementAttribute)
    if focused is not None:
        info["focused_role"] = _ax_copy(focused, _kAXRoleAttribute)

    return info


# --- Screenshot --------------------------------------------------------------


def _encode_cgimage_jpeg(cg_image, quality: float) -> Optional[tuple]:
    """Encode a CGImage to (jpeg_bytes, width, height) via NSBitmapImageRep."""
    width = CGImageGetWidth(cg_image)
    height = CGImageGetHeight(cg_image)
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
    props = {NSImageCompressionFactor: float(quality)}
    data = rep.representationUsingType_properties_(_JPEG_TYPE, props)
    if data is None:
        return None
    return bytes(data), int(width), int(height)


def list_active_displays(max_displays: int = 16) -> list:
    """Return the active display IDs. Index 0 is the main display (the one with
    the menu bar). Falls back to [main display] if enumeration fails."""
    err, displays, count = CGGetActiveDisplayList(max_displays, None, None)
    if err != 0 or not displays:
        return [CGMainDisplayID()]
    return list(displays[:count])


def _display_pixel_size(display_id) -> Optional[tuple]:
    """Native pixel dimensions of a display (handles Retina backing scale)."""
    mode = CGDisplayCopyDisplayMode(display_id)
    if mode is None:
        return None
    return CGDisplayModeGetPixelWidth(mode), CGDisplayModeGetPixelHeight(mode)


class _CaptureBackend:
    """Returns one (index, jpeg_bytes|None, width, height) tuple per display."""

    name = "base"

    def grab(self, quality: float) -> list:
        raise NotImplementedError


class CoreGraphicsBackend(_CaptureBackend):
    """Legacy path: CGDisplayCreateImage (deprecated on macOS 14+ but works)."""

    name = "coregraphics"

    def grab(self, quality: float) -> list:
        out = []
        for i, display_id in enumerate(list_active_displays()):
            cg_image = CGDisplayCreateImage(display_id)
            if cg_image is None:
                out.append((i, None, 0, 0))
                continue
            enc = _encode_cgimage_jpeg(cg_image, quality)
            if enc is None:
                out.append((i, None, 0, 0))
            else:
                out.append((i, enc[0], enc[1], enc[2]))
        return out


def _sck_shareable_content(timeout: float = 5.0):
    """Synchronously fetch SCShareableContent (wraps the async completion API)."""
    box = {}
    done = threading.Event()

    def handler(content, error):
        box["content"] = content
        box["error"] = error
        done.set()

    SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    if not done.wait(timeout):
        return None
    if box.get("error") is not None:
        logger.warning("SCShareableContent error: %s", box["error"])
        return None
    return box.get("content")


def _sck_capture_cgimage(content_filter, config, timeout: float = 5.0):
    """Synchronously capture one CGImage via SCScreenshotManager."""
    box = {}
    done = threading.Event()

    def handler(image, error):
        box["image"] = image
        box["error"] = error
        done.set()

    SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
        content_filter, config, handler
    )
    if not done.wait(timeout):
        return None
    if box.get("error") is not None:
        logger.warning("SCScreenshotManager error: %s", box["error"])
        return None
    return box.get("image")


class ScreenCaptureKitBackend(_CaptureBackend):
    """Modern path: ScreenCaptureKit. Caches the display list and refreshes it
    periodically so monitor hot-plugs are picked up without a fetch every tick."""

    name = "screencapturekit"

    def __init__(self, refresh_every: int = 30) -> None:
        self._refresh_every = max(1, refresh_every)
        self._displays: list = []
        self._tick = 0

    def _displays_now(self) -> list:
        if not self._displays or self._tick % self._refresh_every == 0:
            content = _sck_shareable_content()
            if content is not None:
                self._displays = list(content.displays())
        self._tick += 1
        return self._displays

    def grab(self, quality: float) -> list:
        out = []
        for i, disp in enumerate(self._displays_now()):
            px = _display_pixel_size(disp.displayID()) or (
                int(disp.width()),
                int(disp.height()),
            )
            config = SCStreamConfiguration.alloc().init()
            config.setWidth_(px[0])
            config.setHeight_(px[1])
            config.setShowsCursor_(True)
            content_filter = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                disp, []
            )
            cg_image = _sck_capture_cgimage(content_filter, config)
            if cg_image is None:
                out.append((i, None, 0, 0))
                continue
            enc = _encode_cgimage_jpeg(cg_image, quality)
            if enc is None:
                out.append((i, None, 0, 0))
            else:
                out.append((i, enc[0], enc[1], enc[2]))
        return out


def make_backend() -> _CaptureBackend:
    """Pick the best available capture backend for this macOS version."""
    if _SCK_OK:
        return ScreenCaptureKitBackend()
    logger.info("ScreenCaptureKit unavailable — using CoreGraphics fallback")
    return CoreGraphicsBackend()


def _frame_path(base_dir: Path, ts: _dt.datetime, screen_index: int) -> Path:
    day_dir = base_dir / ts.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{ts.strftime('%H-%M-%S')}_{ts.microsecond // 1000:03d}"
    return day_dir / f"{stamp}_{screen_index}.jpg"


# --- Capture manager ---------------------------------------------------------


@dataclass
class CaptureStats:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    running: bool = False
    frames: int = 0
    errors: int = 0
    last_app: Optional[str] = None
    last_window: Optional[str] = None
    last_error: Optional[str] = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "frames": self.frames,
                "errors": self.errors,
                "last_app": self.last_app,
                "last_window": self.last_window,
                "last_error": self.last_error,
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


class ScreenCaptureManager:
    """Runs a 1 fps screenshot + AX-read loop on a dedicated daemon thread.

    The CoreGraphics screenshot is a blocking call, so this uses a plain thread
    (not the asyncio loop). Call ``start()`` / ``stop()`` / ``toggle()``.
    """

    def __init__(
        self,
        capture_dir: Path = DEFAULT_CAPTURE_DIR,
        interval: float = 1.0,
        quality: float = 0.6,
        write_frames: bool = True,
        backend: Optional[_CaptureBackend] = None,
    ) -> None:
        self.capture_dir = Path(capture_dir)
        self.interval = interval
        self.quality = quality
        self.write_frames = write_frames
        self.backend = backend or make_backend()
        self.stats = CaptureStats()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        if not screen_recording_ok():
            logger.warning(
                "Screen Recording not granted — capture will produce no frames. "
                "Use 'Grant Permissions' / request_permissions() first."
            )
        if not accessibility_ok():
            logger.warning(
                "Accessibility not granted — window titles will be unavailable."
            )
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Screen capture starting (%s backend) — frames -> %s",
            self.backend.name,
            self.capture_dir,
        )
        self._stop.clear()
        self.stats.update(running=True, last_error=None)
        self._thread = threading.Thread(
            target=self._run, name="screen_capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.stats.update(running=False)
        logger.info("Screen capture stopping")

    def toggle(self) -> bool:
        """Flip running state. Returns the new is_running value."""
        if self.is_running:
            self.stop()
            return False
        self.start()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            tick = time.monotonic()
            # An autorelease pool per iteration keeps CoreGraphics/AppKit temp
            # objects from accumulating over a long-running loop.
            with objc.autorelease_pool():
                try:
                    self._capture_once()
                except Exception as e:  # never let the loop die
                    self.stats.update(
                        errors=self.stats.snapshot()["errors"] + 1, last_error=str(e)
                    )
                    logger.error("Capture iteration failed: %s", e, exc_info=True)

            # Pace to the interval, accounting for capture time.
            elapsed = time.monotonic() - tick
            self._stop.wait(max(0.0, self.interval - elapsed))
        self.stats.update(running=False)

    def _capture_once(self) -> None:
        ts = _dt.datetime.now()
        win = read_focused_window()

        # A "frame" is one tick; each tick writes one JPEG per display (_i suffix).
        tick = self.stats.snapshot()["frames"] + 1
        captured = 0
        for i, jpeg, width, height in self.backend.grab(self.quality):
            if jpeg is None:
                self.stats.update(
                    errors=self.stats.snapshot()["errors"] + 1,
                    last_error="null image (Screen Recording not granted?)",
                )
                logger.warning(
                    "Display %d screenshot returned no image (permission?)", i
                )
                continue

            if self.write_frames:
                _frame_path(self.capture_dir, ts, i).write_bytes(jpeg)
            captured += 1
            logger.info(
                "tick #%d display %d %dx%d %.0fKB | app=%s window=%s role=%s",
                tick,
                i,
                width,
                height,
                len(jpeg) / 1024,
                win["app"],
                win["window_title"],
                win["focused_role"],
            )

        if captured:
            self.stats.update(
                frames=tick,
                last_app=win["app"],
                last_window=win["window_title"],
            )


# --- Standalone entry point --------------------------------------------------


def _main() -> None:
    parser = argparse.ArgumentParser(description="macOS screen+AX capture test")
    parser.add_argument(
        "--seconds", type=int, default=0, help="run for N seconds (0 = until Ctrl-C)"
    )
    parser.add_argument("--interval", type=float, default=1.0, help="capture interval")
    parser.add_argument(
        "--no-write", action="store_true", help="don't write JPEGs, just log AX info"
    )
    parser.add_argument(
        "--dir", default=str(DEFAULT_CAPTURE_DIR), help="output directory for frames"
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )

    status = request_permissions()
    if not status["screen_recording"]:
        logger.warning(
            "Screen Recording not yet granted. Approve it in System Settings -> "
            "Privacy & Security -> Screen Recording, then re-run."
        )
    if not status["accessibility"]:
        logger.warning(
            "Accessibility not yet granted. Approve this process in System Settings "
            "-> Privacy & Security -> Accessibility, then re-run."
        )

    mgr = ScreenCaptureManager(
        capture_dir=Path(args.dir),
        interval=args.interval,
        write_frames=not args.no_write,
    )
    mgr.start()
    try:
        if args.seconds > 0:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop()
        time.sleep(args.interval + 0.2)
        logger.info("Final stats: %s", mgr.stats.snapshot())


if __name__ == "__main__":
    _main()
