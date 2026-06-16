"""py2app build for the Chronicle wearable menu bar app (stage 2 .app bundle).

Produces a real ``.app`` with its own bundle identity so macOS TCC permission
grants (Screen Recording, Accessibility, Microphone, Bluetooth) attach to the
*app* — not to whatever terminal/interpreter launched it — and therefore stick.

Build with the build script (recommended, also signs):
    ./build_app.sh

Or directly:
    uv run --with py2app python setup_app.py py2app

Notes:
  - Accessibility has NO Info.plist usage string — it's a manual toggle in
    System Settings, so there's nothing to declare here. The other capabilities
    do need usage strings (below).
  - The app is intentionally NOT sandboxed (no entitlement requesting the
    sandbox): the Accessibility API can't read other apps' windows from inside
    a sandbox. See CAPTURE.md.
"""

from setuptools import setup

APP = ["menu_app.py"]

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
    # Bundle our own module + the heavier runtime deps explicitly; py2app's
    # pyobjc recipes pick up AppKit/Quartz/etc. automatically.
    "includes": ["screen_capture"],
    "packages": ["rumps", "bleak", "dotenv", "yaml"],
}

setup(
    app=APP,
    name="Chronicle Wearable",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
