"""macOS window-server policy for a menu-bar-only tray.

The tray runs as a plain (non-bundled) Python process, so AppKit treats it as a
regular application: a "Python" Dock tile that invites the user to quit the
thing that is supposed to be running quietly in the menu bar. Setting the
activation policy to *accessory* removes the tile and keeps the menu bar item.

Every function is a no-op off macOS, and when pyobjc is unavailable.
"""

import logging
import sys

logger = logging.getLogger(__name__)

_NS_ACCESSORY = 1  # NSApplicationActivationPolicyAccessory


def _shared_app():
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSApplication
    except ImportError:
        return None
    return NSApplication.sharedApplication()


def hide_dock_icon() -> None:
    """Drop the Dock tile, keeping the menu bar item.

    Call this *after* QApplication is constructed. Qt's cocoa plugin promotes a
    non-bundled executable to a foreground app while it initialises, so a policy
    set beforehand is silently reverted to `Regular` and the Dock tile returns.
    """
    app = _shared_app()
    if app is None:
        return
    app.setActivationPolicy_(_NS_ACCESSORY)


def activate_for_dialog() -> None:
    """Bring this process forward before showing a modal dialog.

    An accessory app is never activated by clicking the menu bar item, so its
    dialogs open behind whatever the user was working in and take no keyboard
    focus.
    """
    app = _shared_app()
    if app is None:
        return
    app.activateIgnoringOtherApps_(True)
