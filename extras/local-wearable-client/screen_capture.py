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

Two macOS permissions are required (TCC):
  - Screen Recording  -> `CGRequestScreenCaptureAccess()`
  - Accessibility     -> `AXIsProcessTrustedWithOptions({prompt: True})`

The grant attaches to the *responsible process*. Run from a terminal it's the
terminal; run under the launchd agent (the daily deployment) it's the agent's
own Python. See CAPTURE.md for deployment + permissions.

Standalone test:
    uv run python screen_capture.py            # 1 fps, prints focused window, Ctrl-C to stop
    uv run python screen_capture.py --seconds 10 --ocr
"""

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
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
from Foundation import NSData
from Quartz import (
    CGBitmapContextCreate,
    CGBitmapContextCreateImage,
    CGColorSpaceCreateDeviceRGB,
    CGContextDrawImage,
    CGContextSetInterpolationQuality,
    CGDisplayCopyDisplayMode,
    CGDisplayCreateImage,
    CGDisplayModeGetPixelHeight,
    CGDisplayModeGetPixelWidth,
    CGGetActiveDisplayList,
    CGImageGetHeight,
    CGImageGetWidth,
    CGMainDisplayID,
    CGPreflightScreenCaptureAccess,
    CGRectMake,
    CGRequestScreenCaptureAccess,
    kCGImageAlphaPremultipliedLast,
    kCGInterpolationHigh,
    kCGInterpolationLow,
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

# User-idle time (seconds since last HID input) — used to attribute foreground
# time to "idle" rather than the focused app during analytics.
try:
    from Quartz import (
        CGEventSourceSecondsSinceLastEventType,
        kCGAnyInputEventType,
        kCGEventSourceStateHIDSystemState,
    )

    _IDLE_OK = True
except Exception:  # pragma: no cover
    _IDLE_OK = False

# Screen-lock / screensaver detection — skip capturing a locked screen.
try:
    from Quartz import CGSessionCopyCurrentDictionary

    _LOCK_OK = True
except Exception:  # pragma: no cover
    _LOCK_OK = False

# Apple Vision OCR is optional (opt-in via CAPTURE_OCR) — it's CPU-heavy at 1 fps.
try:
    import Vision

    _VISION_OK = True
except Exception:  # pragma: no cover
    Vision = None
    _VISION_OK = False

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
_kAXSubroleAttribute = "AXSubrole"
_kAXSelectedTextAttribute = "AXSelectedText"
_kAXURLAttribute = "AXURL"
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


# JPEG quality for the full-res frame fed to OCR (not saved to disk). High, so
# text edges stay crisp for recognition; the on-disk frames use ``quality``.
OCR_JPEG_QUALITY = 0.9


# --- Idle time + OCR ----------------------------------------------------------


def user_idle_seconds() -> Optional[float]:
    """Seconds since the last user HID input (mouse/keyboard). None if the API
    is unavailable. Used to attribute foreground time to "idle" in analytics."""
    if not _IDLE_OK:
        return None
    try:
        return float(
            CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
            )
        )
    except Exception:
        return None


def screen_is_locked() -> bool:
    """True if the screen is locked / screensaver is up. We skip capturing then
    (a locked screen has nothing useful and ScreenCaptureKit may stall)."""
    if not _LOCK_OK:
        return False
    try:
        info = CGSessionCopyCurrentDictionary()
        if not info:
            return False
        return bool(info.get("CGSSessionScreenIsLocked", 0))
    except Exception:
        return False


def ocr_jpeg(jpeg_bytes: bytes, fast: bool = True) -> Optional[str]:
    """Run Apple Vision text recognition on JPEG bytes. Returns the recognized
    text (newline-joined), or None if Vision is unavailable / found nothing.

    Synchronous: ``performRequests:error:`` blocks, so results are ready on the
    request afterwards (no completion handler needed)."""
    if not _VISION_OK or not jpeg_bytes:
        return None
    try:
        data = NSData.dataWithBytes_length_(jpeg_bytes, len(jpeg_bytes))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        # 0 = accurate, 1 = fast. Fast + no language correction for 1 fps.
        request.setRecognitionLevel_(1 if fast else 0)
        request.setUsesLanguageCorrection_(not fast)
        handler.performRequests_error_([request], None)
        observations = request.results() or []
        lines = []
        for obs in observations:
            candidates = obs.topCandidates_(1)
            if candidates:
                lines.append(candidates[0].string())
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.warning("OCR failed: %s", e)
        return None


# --- Accessibility: focused window read --------------------------------------


def _ax_copy(element, attribute: str):
    """Wrapper over AXUIElementCopyAttributeValue returning the value or None."""
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if err != kAXErrorSuccess:
        return None
    return value


def read_focused_window() -> dict:
    """Read the frontmost app + focused-window context via the AX API.

    Returns: app, bundle_id, pid, window_title, focused_role, focused_subrole,
    url (best-effort; browsers/document apps that expose AXURL), and
    selected_text_len (length only — the content itself is not stored).

    Missing pieces come back as None/0 (e.g. if AX isn't granted, titles are
    None but the app name still resolves via NSWorkspace).
    """
    info = {
        "app": None,
        "bundle_id": None,
        "pid": None,
        "window_title": None,
        "focused_role": None,
        "focused_subrole": None,
        "url": None,
        "selected_text_len": 0,
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
        info["focused_subrole"] = _ax_copy(focused, _kAXSubroleAttribute)
        selected = _ax_copy(focused, _kAXSelectedTextAttribute)
        if isinstance(selected, str):
            info["selected_text_len"] = len(selected)

    # Best-effort URL: browsers/document apps expose AXURL on the window (or the
    # focused element). Shallow check only — no full UI-tree walk.
    url = _ax_copy(window, _kAXURLAttribute) if window is not None else None
    if url is None and focused is not None:
        url = _ax_copy(focused, _kAXURLAttribute)
    if url is not None:
        try:
            info["url"] = url.absoluteString()
        except Exception:
            info["url"] = str(url)

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


def _scaled_cgimage(cg_image, target_w: int, target_h: int, interpolation):
    """Return a CGImage redrawn at target_w x target_h, or None on failure."""
    target_w = max(1, int(target_w))
    target_h = max(1, int(target_h))
    color_space = CGColorSpaceCreateDeviceRGB()
    # data=None lets CoreGraphics allocate; bytesPerRow=0 lets it pick the stride.
    ctx = CGBitmapContextCreate(
        None, target_w, target_h, 8, 0, color_space, kCGImageAlphaPremultipliedLast
    )
    if ctx is None:
        return None
    CGContextSetInterpolationQuality(ctx, interpolation)
    CGContextDrawImage(ctx, CGRectMake(0, 0, target_w, target_h), cg_image)
    return CGBitmapContextCreateImage(ctx)


def _thumb_hash(cg_image, thumb_max: int) -> Optional[str]:
    """SHA-1 of a small downscaled thumbnail of the image. Hashing a thumbnail
    (rather than the full-res JPEG) is cheap and ignores trivial pixel noise, so
    near-identical frames dedup reliably. Returns None if scaling fails."""
    src_w = CGImageGetWidth(cg_image)
    src_h = CGImageGetHeight(cg_image)
    if src_w == 0 or src_h == 0:
        return None
    scale = min(1.0, thumb_max / float(max(src_w, src_h)))
    thumb = _scaled_cgimage(cg_image, src_w * scale, src_h * scale, kCGInterpolationLow)
    if thumb is None:
        return None
    enc = _encode_cgimage_jpeg(thumb, 0.4)
    if enc is None:
        return None
    return hashlib.sha1(enc[0]).hexdigest()


def _finish_frame(cg_image, quality: float, thumb_max: int) -> Optional[tuple]:
    """Encode a (already scaled) CGImage and compute its dedup hash.
    Returns (jpeg_bytes, width, height, thumb_hash) or None."""
    enc = _encode_cgimage_jpeg(cg_image, quality)
    if enc is None:
        return None
    jpeg, width, height = enc
    return jpeg, width, height, _thumb_hash(cg_image, thumb_max)


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
    """Returns one (index, jpeg_bytes|None, width, height, thumb_hash) tuple per
    display. Frames are saved at ``scale`` of native resolution; ``thumb_max`` is
    the max dimension of the thumbnail used for the dedup hash."""

    name = "base"

    def grab(self, quality: float, scale: float, thumb_max: int) -> list:
        raise NotImplementedError

    def grab_full(self, index: int, quality: float) -> Optional[bytes]:
        """Capture a single display at native (full) resolution and return JPEG
        bytes. Used only for OCR on changed frames — the saved frame stays
        downscaled. Returns None if the display/capture is unavailable."""
        return None


class CoreGraphicsBackend(_CaptureBackend):
    """Legacy path: CGDisplayCreateImage (deprecated on macOS 14+ but works).
    Captures at native resolution, then downscales to ``scale`` before encoding."""

    name = "coregraphics"

    def grab(self, quality: float, scale: float, thumb_max: int) -> list:
        out = []
        for i, display_id in enumerate(list_active_displays()):
            cg_image = CGDisplayCreateImage(display_id)
            if cg_image is None:
                out.append((i, None, 0, 0, None))
                continue
            if scale < 1.0:
                scaled = _scaled_cgimage(
                    cg_image,
                    CGImageGetWidth(cg_image) * scale,
                    CGImageGetHeight(cg_image) * scale,
                    kCGInterpolationHigh,
                )
                if scaled is not None:
                    cg_image = scaled
            enc = _finish_frame(cg_image, quality, thumb_max)
            out.append((i, *enc) if enc else (i, None, 0, 0, None))
        return out

    def grab_full(self, index: int, quality: float) -> Optional[bytes]:
        displays = list_active_displays()
        if index >= len(displays):
            return None
        cg_image = CGDisplayCreateImage(displays[index])
        if cg_image is None:
            return None
        enc = _encode_cgimage_jpeg(cg_image, quality)
        return enc[0] if enc else None


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

    def grab(self, quality: float, scale: float, thumb_max: int) -> list:
        out = []
        for i, disp in enumerate(self._displays_now()):
            px = _display_pixel_size(disp.displayID()) or (
                int(disp.width()),
                int(disp.height()),
            )
            config = SCStreamConfiguration.alloc().init()
            # Capture directly at the target (scaled) resolution — ScreenCaptureKit
            # downscales in the compositor, so we never produce the full-res bitmap.
            config.setWidth_(max(1, int(px[0] * scale)))
            config.setHeight_(max(1, int(px[1] * scale)))
            config.setShowsCursor_(True)
            content_filter = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                disp, []
            )
            cg_image = _sck_capture_cgimage(content_filter, config)
            if cg_image is None:
                out.append((i, None, 0, 0, None))
                continue
            enc = _finish_frame(cg_image, quality, thumb_max)
            out.append((i, *enc) if enc else (i, None, 0, 0, None))
        return out

    def grab_full(self, index: int, quality: float) -> Optional[bytes]:
        # Reuse the display list cached by the grab() earlier this tick; don't
        # bump _tick so the periodic refresh cadence is unaffected.
        displays = self._displays
        if index >= len(displays):
            return None
        disp = displays[index]
        px = _display_pixel_size(disp.displayID()) or (
            int(disp.width()),
            int(disp.height()),
        )
        config = SCStreamConfiguration.alloc().init()
        config.setWidth_(max(1, int(px[0])))
        config.setHeight_(max(1, int(px[1])))
        config.setShowsCursor_(True)
        content_filter = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            disp, []
        )
        cg_image = _sck_capture_cgimage(content_filter, config)
        if cg_image is None:
            return None
        enc = _encode_cgimage_jpeg(cg_image, quality)
        return enc[0] if enc else None


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


# --- Video compaction (JPEG -> HEVC) -----------------------------------------

# Frame filenames are "<HH>-<MM>-<SS>_<mmm>_<index>.jpg" (see _frame_path).
_FRAME_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})_(\d{3})_(\d+)\.jpg$")
# The launchd agent runs with a minimal PATH that omits Homebrew's bin dirs.
_BIN_FALLBACKS = ("/opt/homebrew/bin", "/usr/local/bin")


def _which(name: str) -> Optional[str]:
    """Resolve a binary by PATH, falling back to common Homebrew locations."""
    found = shutil.which(name)
    if found:
        return found
    for d in _BIN_FALLBACKS:
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return None


def battery_ok(min_pct: int = 20) -> bool:
    """True if it's safe to run a CPU/GPU burst now: on AC power, on a desktop
    with no battery, or on battery with charge >= ``min_pct``. Unknown -> True
    (don't block work just because the battery couldn't be read)."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return True
    if "AC Power" in out:
        return True
    m = re.search(r"(\d+)%", out)
    return int(m.group(1)) >= min_pct if m else True


def _pointer_fields(pointer) -> dict:
    """Expand a stored frame pointer into event ``displays[]`` fields. A raw
    pointer is a relative ``.jpg`` path (or None); a compacted pointer is a
    ``{"video","frame"}`` dict referencing a frame inside an HEVC chunk."""
    if isinstance(pointer, dict):
        return {"file": None, "video": pointer["video"], "frame": pointer["frame"]}
    return {"file": pointer}


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
        write_events: bool = True,
        ocr: bool = False,
        dedup: bool = True,
        skip_idle_secs: float = 90.0,
        retention_days: int = 14,
        save_scale: float = 0.5,
        thumb_max: int = 256,
        compact_every_mins: int = 30,
        compact_after_secs: float = 600.0,
        compact_quality: int = 60,
        compact_min_battery: int = 20,
        backend: Optional[_CaptureBackend] = None,
    ) -> None:
        self.capture_dir = Path(capture_dir)
        self.interval = interval
        self.quality = quality
        self.write_frames = write_frames
        self.write_events = write_events
        self.ocr = ocr
        # Fraction of native resolution to save frames at (0.5 = half res); full
        # res is ~1.4MB/frame, half res is roughly a quarter of that.
        self.save_scale = max(0.05, min(1.0, save_scale))
        # Max dimension of the thumbnail used for the dedup hash.
        self.thumb_max = max(16, int(thumb_max))
        # --- Storage controls (see CAPTURE.md "Storage") ---
        self.dedup = dedup  # skip writing frames identical to the last stored one
        self.skip_idle_secs = skip_idle_secs  # >0: skip screenshots while idle
        self.retention_days = (
            retention_days  # delete screenshots older than this (0=keep)
        )
        # --- Compaction: collapse old JPEGs into HEVC video (see CAPTURE.md) ---
        self.compact_every_mins = max(0, int(compact_every_mins))  # 0 = disabled
        self.compact_after_secs = max(60.0, float(compact_after_secs))
        self.compact_quality = max(0, min(100, int(compact_quality)))
        self.compact_min_battery = max(0, min(100, int(compact_min_battery)))
        self._ffmpeg = _which("ffmpeg")
        self._ffprobe = _which("ffprobe")
        self._last_compact: float = 0.0
        if self.compact_every_mins > 0 and not self._ffmpeg:
            logger.warning(
                "Compaction enabled but ffmpeg not found — disabling. "
                "Install it (brew install ffmpeg) to compact frames to video."
            )
            self.compact_every_mins = 0
        self.backend = backend or make_backend()
        self.stats = CaptureStats()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Per-day events.jsonl handle (rotated by date in _append_event).
        self._events_fh = None
        self._events_date: Optional[str] = None
        # Dedup state per display index: last stored hash / file / ocr_file.
        self._last_hash: dict = {}
        self._last_file: dict = {}
        self._last_ocr: dict = {}
        self._last_sweep: float = 0.0

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
            "Screen capture starting (%s backend) — frames -> %s "
            "[dedup=%s skip_idle=%ss retention=%sd compact=%s]",
            self.backend.name,
            self.capture_dir,
            self.dedup,
            self.skip_idle_secs,
            self.retention_days,
            f"every {self.compact_every_mins}min @q{self.compact_quality}"
            if self.compact_every_mins > 0
            else "off",
        )
        self._cleanup_compaction_temps()
        self._sweep_retention()
        self._stop.clear()
        self.stats.update(running=True, last_error=None)
        self._thread = threading.Thread(
            target=self._run, name="screen_capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.stats.update(running=False)
        if self._events_fh is not None:
            try:
                self._events_fh.close()
            except Exception:
                pass
            self._events_fh = None
            self._events_date = None
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

            # Sweep old screenshots roughly hourly.
            if time.monotonic() - self._last_sweep > 3600:
                self._sweep_retention()

            # Compact old JPEGs into HEVC on a cadence, but only while power
            # allows. If the battery is too low we skip without stamping
            # _last_compact, so it runs as soon as the machine is on AC again.
            if (
                self.compact_every_mins > 0
                and time.monotonic() - self._last_compact
                >= self.compact_every_mins * 60
                and battery_ok(self.compact_min_battery)
            ):
                try:
                    self._compact_frames()
                except Exception as e:  # never let the loop die
                    logger.error("Compaction failed: %s", e, exc_info=True)
                finally:
                    self._last_compact = time.monotonic()

            # Pace to the interval, accounting for capture time.
            elapsed = time.monotonic() - tick
            self._stop.wait(max(0.0, self.interval - elapsed))
        self.stats.update(running=False)

    def _sweep_retention(self) -> None:
        """Delete screenshots (.jpg), HEVC chunks (.mp4) and OCR (.txt) older
        than retention_days. events.jsonl is always kept — it's tiny and powers
        analytics."""
        self._last_sweep = time.monotonic()
        if self.retention_days <= 0:
            return
        cutoff = _dt.date.today() - _dt.timedelta(days=self.retention_days)
        deleted = 0
        try:
            for d in self.capture_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    day = _dt.date.fromisoformat(d.name)
                except ValueError:
                    continue
                if day >= cutoff:
                    continue
                for pattern in ("*.jpg", "*.txt", "*.mp4"):
                    for f in d.glob(pattern):
                        f.unlink(missing_ok=True)
                        deleted += 1
        except Exception as e:
            logger.warning("Retention sweep failed: %s", e)
        if deleted:
            logger.info(
                "Retention: deleted %d screenshot/chunk/OCR files older than %d days",
                deleted,
                self.retention_days,
            )

    # --- Compaction (JPEG -> HEVC) ----------------------------------------

    def _cleanup_compaction_temps(self) -> None:
        """Remove leftovers from an interrupted compaction run (incomplete
        chunks, list files, half-written event rewrites)."""
        try:
            for d in self.capture_dir.iterdir():
                if not d.is_dir():
                    continue
                for pattern in ("*.mp4.part", ".compact_*.txt", "events.jsonl.tmp"):
                    for f in d.glob(pattern):
                        f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Compaction temp cleanup failed: %s", e)

    def _load_events(self, path: Path) -> list:
        """Read a day's events.jsonl into a list of dicts (empty if missing)."""
        events: list = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Could not read %s for compaction: %s", path, e)
        return events

    @staticmethod
    def _dims_from_events(events: list) -> dict:
        """Map each raw-frame relative path -> (w, h) from event entries."""
        dims: dict = {}
        for ev in events:
            for d in ev.get("displays", []):
                f = d.get("file")
                if f:
                    dims[f] = (d.get("w"), d.get("h"))
        return dims

    def _compact_frames(self) -> None:
        """Collapse JPEGs older than ``compact_after_secs`` into per-display HEVC
        chunks, rewrite the events.jsonl pointers, then delete the JPEGs. Runs on
        the capture thread, so it shares dedup state and the events handle with
        the writer (no locking needed). Ordering is crash-safe: chunk -> events
        rewrite -> dedup-pointer fixup -> delete JPEGs."""
        if not self._ffmpeg:
            return
        cutoff = time.time() - self.compact_after_secs
        for daydir in sorted(p for p in self.capture_dir.iterdir() if p.is_dir()):
            try:
                day = _dt.date.fromisoformat(daydir.name)
            except ValueError:
                continue

            # Gather compactable JPEGs (older than cutoff), grouped by display.
            groups: dict = {}
            for f in daydir.glob("*.jpg"):
                m = _FRAME_RE.match(f.name)
                if not m:
                    continue
                hh, mm, ss, ms, idx = m.groups()
                epoch = _dt.datetime.combine(
                    day, _dt.time(int(hh), int(mm), int(ss), int(ms) * 1000)
                ).timestamp()
                if epoch >= cutoff:
                    continue
                groups.setdefault(int(idx), []).append(f.name)
            if not groups:
                continue

            events = self._load_events(daydir / "events.jsonl")
            dims = self._dims_from_events(events)

            mapping: dict = {}  # "<date>/<name>.jpg" -> {"video":..., "frame":..}
            for idx in sorted(groups):
                names = sorted(groups[idx])  # chronological (fixed-width stamps)
                for run in self._split_by_dims(daydir.name, names, dims):
                    self._encode_chunk(daydir, idx, run, mapping)
            if not mapping:
                continue

            self._rewrite_events(daydir / "events.jsonl", daydir.name, events, mapping)

            # Keep live dedup pointers valid if they referenced a compacted JPEG.
            for i, ptr in list(self._last_file.items()):
                if isinstance(ptr, str) and ptr in mapping:
                    self._last_file[i] = mapping[ptr]

            # Delete the now-compacted JPEGs (keep .txt OCR sidecars).
            for rel in mapping:
                (self.capture_dir / rel).unlink(missing_ok=True)
            logger.info(
                "Compacted %d frames in %s into HEVC (%d chunk(s))",
                len(mapping),
                daydir.name,
                len({m["video"] for m in mapping.values()}),
            )

    @staticmethod
    def _split_by_dims(date_str: str, names: list, dims: dict) -> list:
        """Split a chronological list of frame names into runs of equal (w,h);
        a resolution change mid-stream would corrupt a single HEVC stream."""
        runs: list = []
        cur: list = []
        cur_dim = object()  # sentinel != any real (w,h) or None
        for name in names:
            dim = dims.get(f"{date_str}/{name}")
            if cur and dim != cur_dim:
                runs.append(cur)
                cur = []
            cur.append(name)
            cur_dim = dim
        if cur:
            runs.append(cur)
        return runs

    def _encode_chunk(self, daydir: Path, idx: int, names: list, mapping: dict) -> None:
        """Encode one resolution-uniform run of JPEGs into an HEVC chunk and add
        its frame pointers to ``mapping``. No-op (leaves JPEGs) on any failure."""
        if not names:
            return
        stem = names[0][: -len(".jpg")]  # "<HH-MM-SS_mmm>_<idx>"
        suffix = f"_{idx}"
        time_part = stem[: -len(suffix)] if stem.endswith(suffix) else stem
        chunk_name = f"screen{idx}_{time_part}.mp4"
        list_name = f".compact_{idx}_{time_part}.txt"
        list_path = daydir / list_name
        part_path = daydir / (chunk_name + ".part")
        chunk_path = daydir / chunk_name
        try:
            list_path.write_text(
                "".join(f"file '{n}'\n" for n in names), encoding="utf-8"
            )
            cmd = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-r", "1", "-f", "concat", "-safe", "0", "-i", list_name,
                "-c:v", "hevc_videotoolbox", "-q:v", str(self.compact_quality),
                "-bf", "0", "-g", "30", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
                "-fps_mode", "cfr", "-frames:v", str(len(names)),
                # Force the muxer: the output name ends in .part, so ffmpeg can't
                # infer mp4 from the extension.
                "-f", "mp4", chunk_name + ".part",
            ]
            res = subprocess.run(
                cmd, cwd=str(daydir), capture_output=True, text=True, timeout=300
            )
            if res.returncode != 0 or not part_path.exists():
                logger.warning(
                    "ffmpeg failed for %s/%s: %s",
                    daydir.name,
                    chunk_name,
                    res.stderr.strip()[:300],
                )
                return
            if not self._verify_chunk(part_path, len(names)):
                logger.warning(
                    "Chunk %s/%s frame-count mismatch — skipping",
                    daydir.name,
                    chunk_name,
                )
                return
            os.replace(part_path, chunk_path)
            video_rel = f"{daydir.name}/{chunk_name}"
            for k, name in enumerate(names):
                mapping[f"{daydir.name}/{name}"] = {"video": video_rel, "frame": k}
        except Exception as e:
            logger.warning("Encoding %s/%s failed: %s", daydir.name, chunk_name, e)
        finally:
            list_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)

    def _verify_chunk(self, chunk: Path, expected: int) -> bool:
        """True if the encoded chunk decodes to exactly ``expected`` frames.
        Without ffprobe we trust ffmpeg's exit code + the -frames:v cap."""
        if not self._ffprobe:
            return True
        try:
            res = subprocess.run(
                [
                    self._ffprobe, "-v", "error", "-count_frames",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_frames",
                    "-of", "default=nokey=1:noprint_wrappers=1", str(chunk),
                ],
                capture_output=True, text=True, timeout=120,
            )
            return res.stdout.strip() == str(expected)
        except Exception:
            return False

    def _rewrite_events(
        self, path: Path, date_str: str, events: list, mapping: dict
    ) -> None:
        """Rewrite a day's events.jsonl, repointing compacted frames at their
        chunk (file -> null, add video/frame). Atomic (temp + os.replace); if
        it's the day currently being appended, the open handle is closed first
        and lazily reopened by _append_event (an fd left open on the replaced
        inode would silently drop subsequent appends)."""
        changed = False
        for ev in events:
            for d in ev.get("displays", []):
                f = d.get("file")
                if f in mapping:
                    m = mapping[f]
                    d["file"] = None
                    d["video"] = m["video"]
                    d["frame"] = m["frame"]
                    changed = True
        if not changed:
            return
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if self._events_fh is not None and self._events_date == date_str:
            try:
                self._events_fh.close()
            except Exception:
                pass
            self._events_fh = None
            self._events_date = None
        os.replace(tmp, path)

    def _append_event(self, ts: _dt.datetime, event: dict) -> None:
        """Append one JSON object (one line) to the per-day events.jsonl."""
        date = ts.strftime("%Y-%m-%d")
        if self._events_date != date or self._events_fh is None:
            if self._events_fh is not None:
                self._events_fh.close()
            path = self.capture_dir / date / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._events_fh = open(path, "a", encoding="utf-8")
            self._events_date = date
        self._events_fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events_fh.flush()

    def _capture_once(self) -> None:
        ts = _dt.datetime.now()
        win = read_focused_window()
        idle = user_idle_seconds()

        # A "frame" is one tick. Storage controls (see CAPTURE.md "Storage"):
        #  - skip_idle: while idle >= threshold the screen isn't changing, so
        #    skip screenshots entirely; still log the event for the timeline.
        #  - dedup: skip writing a frame identical to the last stored one and
        #    reuse that file in the event (transparent to readers).
        tick = self.stats.snapshot()["frames"] + 1
        idle_skip = (
            self.skip_idle_secs > 0 and idle is not None and idle >= self.skip_idle_secs
        )
        if screen_is_locked():
            skip_reason = "skipped_locked"
        elif idle_skip:
            skip_reason = "skipped_idle"
        else:
            skip_reason = None

        captured = 0
        displays_meta = []
        if skip_reason is None:
            for i, jpeg, width, height, phash in self.backend.grab(
                self.quality, self.save_scale, self.thumb_max
            ):
                if jpeg is None:
                    self.stats.update(
                        errors=self.stats.snapshot()["errors"] + 1,
                        last_error="null image (Screen Recording not granted?)",
                    )
                    logger.warning(
                        "Display %d screenshot returned no image (permission?)", i
                    )
                    continue

                # Dedup on a downscaled-thumbnail hash (fall back to the full
                # frame if thumbnailing failed, so we never falsely dedup).
                digest = phash or hashlib.sha1(jpeg).hexdigest()
                unchanged = self.dedup and self._last_hash.get(i) == digest

                if unchanged:
                    # The pointer may be a raw .jpg path or, if the previous
                    # frame was already compacted, a {"video","frame"} dict.
                    pointer = self._last_file.get(i)
                    ocr_file = self._last_ocr.get(i)
                else:
                    frame_p = _frame_path(self.capture_dir, ts, i)
                    pointer = None
                    if self.write_frames:
                        frame_p.write_bytes(jpeg)
                        pointer = str(frame_p.relative_to(self.capture_dir))
                    ocr_file = None
                    if self.ocr:
                        # OCR the full-res frame (the saved jpeg is downscaled and
                        # too soft for reliable recognition). Captured separately,
                        # only on changed frames; fall back to the saved frame if
                        # the full-res grab fails. Accurate mode + language
                        # correction since we only pay this on real changes.
                        full = self.backend.grab_full(i, OCR_JPEG_QUALITY)
                        text = ocr_jpeg(full or jpeg, fast=False)
                        if text:
                            txt_p = frame_p.with_suffix(".txt")
                            txt_p.write_text(text, encoding="utf-8")
                            ocr_file = str(txt_p.relative_to(self.capture_dir))
                    self._last_hash[i] = digest
                    self._last_file[i] = pointer
                    self._last_ocr[i] = ocr_file

                displays_meta.append(
                    {
                        "index": i,
                        **_pointer_fields(pointer),
                        "w": width,
                        "h": height,
                        "ocr_file": ocr_file,
                        "changed": not unchanged,
                    }
                )
                captured += 1
                logger.info(
                    "tick #%d display %d %dx%d %.0fKB %s | app=%s window=%s",
                    tick,
                    i,
                    width,
                    height,
                    len(jpeg) / 1024,
                    "new" if not unchanged else "dup",
                    win["app"],
                    win["window_title"],
                )

        # A genuine capture failure (not a deliberate skip but nothing captured)
        # is an error state — don't log a misleading event.
        if skip_reason is None and captured == 0:
            return

        self.stats.update(
            frames=tick,
            last_app=win["app"],
            last_window=win["window_title"],
        )

        if self.write_events:
            event = {
                "ts": ts.isoformat(timespec="milliseconds"),
                "epoch": round(ts.timestamp(), 3),
                "app": win["app"],
                "bundle_id": win["bundle_id"],
                "pid": win["pid"],
                "window_title": win["window_title"],
                "url": win["url"],
                "focused_role": win["focused_role"],
                "focused_subrole": win["focused_subrole"],
                "selected_text_len": win["selected_text_len"],
                "idle_seconds": round(idle, 1) if idle is not None else None,
                "screenshots": skip_reason or "captured",
                "displays": displays_meta,
            }
            try:
                self._append_event(ts, event)
            except Exception as e:
                logger.warning("Failed to write event: %s", e)


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
        "--no-events",
        action="store_true",
        help="don't write the events.jsonl metadata sidecar",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="run Apple Vision OCR on each frame (writes .txt sidecars; CPU-heavy)",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="store every frame even if identical to the last (default: dedup)",
    )
    parser.add_argument(
        "--skip-idle",
        type=float,
        default=90.0,
        help="skip screenshots while idle >= N seconds (0 disables; default 90)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=14,
        help="delete screenshots older than N days (0 = keep forever; default 14)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=0.5,
        help="save frames at this fraction of native resolution (default 0.5 = half)",
    )
    parser.add_argument(
        "--thumb-max",
        type=int,
        default=256,
        help="max dimension of the thumbnail used for the dedup hash (default 256)",
    )
    parser.add_argument(
        "--compact-every-mins",
        type=int,
        default=30,
        help="compact old JPEGs to HEVC every N minutes (0 disables; default 30)",
    )
    parser.add_argument(
        "--compact-after",
        type=float,
        default=600.0,
        help="only compact frames older than N seconds (default 600)",
    )
    parser.add_argument(
        "--compact-quality",
        type=int,
        default=60,
        help="HEVC quality 0-100 for compaction (default 60)",
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
        write_events=not args.no_events,
        ocr=args.ocr,
        dedup=not args.no_dedup,
        skip_idle_secs=args.skip_idle,
        retention_days=args.retention_days,
        save_scale=args.scale,
        thumb_max=args.thumb_max,
        compact_every_mins=args.compact_every_mins,
        compact_after_secs=args.compact_after,
        compact_quality=args.compact_quality,
    )
    if args.ocr and not _VISION_OK:
        logger.warning("--ocr requested but pyobjc-framework-Vision not available")
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
