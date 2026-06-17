"""py2app build for the Chronicle wearable menu bar app (stage 2 .app bundle).

Run via ``../build_app.sh`` (which also code-signs and renames the output).

This lives in an isolated subdir on purpose: py2app rejects the PEP 621
``[project].dependencies`` of the package's own ``pyproject.toml`` (it becomes
``install_requires``, which py2app errors on). Building from a directory with no
``pyproject.toml`` sidesteps that. We add the package dir to ``sys.path`` so
py2app's module graph can still find ``menu_app`` / ``screen_capture`` / ``main``.

Produces a real ``.app`` with its own bundle identity so macOS TCC permission
grants (Screen Recording, Accessibility, Microphone, Bluetooth) attach to the
*app* — not to whatever terminal/interpreter launched it — and therefore stick.

Notes:
  - Accessibility has NO Info.plist usage string — it's a manual toggle in
    System Settings, so there's nothing to declare here. The other capabilities
    do need usage strings (below).
  - The app is intentionally NOT sandboxed: the Accessibility API can't read
    other apps' windows from inside a sandbox. See ../CAPTURE.md.
"""

import os
import sys

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_DIR)

from setuptools import setup  # noqa: E402

APP = [os.path.join(_PKG_DIR, "menu_app.py")]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Chronicle Wearable",
        "CFBundleDisplayName": "Chronicle Wearable",
        "CFBundleIdentifier": "ai.chronicle.wearable",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        # Menu-bar / agent app: no Dock icon, no main window.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        # TCC usage strings (shown in the permission prompts).
        "NSScreenCaptureUsageDescription": (
            "Chronicle captures periodic screenshots to build your personal timeline."
        ),
        "NSMicrophoneUsageDescription": (
            "Chronicle records audio from your wearable and Mac for transcription."
        ),
        "NSBluetoothAlwaysUsageDescription": (
            "Chronicle connects to your OMI/Neo wearable over Bluetooth."
        ),
        "NSAppleEventsUsageDescription": (
            "Chronicle detects the active application to label captures."
        ),
    },
    # Our own modules + the heavier runtime deps. py2app's pyobjc recipes pick up
    # AppKit/Quartz/ScreenCaptureKit/ApplicationServices automatically.
    "includes": ["screen_capture", "main"],
    "packages": ["rumps", "bleak", "dotenv", "yaml"],
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
)
