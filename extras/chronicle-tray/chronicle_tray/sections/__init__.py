"""Tray sections — modular menu blocks.

Each capability (vault sync, ScreenPipe, pendant) is a Section. The tray shell
asks every section whether it's available on this machine; available sections
contribute menu items and get refreshed on a timer, unavailable ones collapse
to a single disabled hint line so the user can see what's missing and how to
enable it.
"""

from PySide6.QtWidgets import QMenu


class Section:
    """One capability block in the tray menu."""

    title: str = ""

    def available(self) -> tuple[bool, str]:
        """(available, hint). When not available, ``hint`` is shown as a
        disabled menu line (e.g. "install syncthing: brew install syncthing")."""
        raise NotImplementedError

    def build(self, menu: QMenu) -> None:
        """Add this section's items to the menu (called once at startup)."""
        raise NotImplementedError

    def refresh(self) -> None:
        """Update live state (called every few seconds by the shell)."""

    def tooltip(self) -> str:
        """One line for the tray tooltip ('' to omit)."""
        return ""

    def warning(self) -> bool:
        """Whether this section needs attention in the persistent tray icon."""
        return False

    def shutdown(self) -> None:
        """Clean up background resources on quit."""
