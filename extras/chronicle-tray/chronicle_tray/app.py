"""Chronicle tray shell — one tray icon, sections decide what's in the menu.

Cross-platform: QSystemTrayIcon renders in the Linux system tray and the macOS
menu bar alike. Sections that aren't usable on this machine (missing binary,
extra not installed) collapse to a disabled hint line instead of disappearing,
so the tray doubles as a "what could this client node do" checklist.
"""

import logging
import sys

from chronicle_tray.logs import configure_logging, log_buffer
from chronicle_tray.sections.pendant import PendantSection
from chronicle_tray.sections.screenpipe import ScreenPipeSection
from chronicle_tray.sections.vault import VaultSection
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSystemTrayIcon,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


def _fallback_icon() -> QIcon:
    """Drawn glyph for platforms without a freedesktop icon theme (macOS)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#c96442"))  # Chronicle terracotta
    font = painter.font()
    font.setPixelSize(52)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), 0x84, "◈")  # AlignCenter
    painter.end()
    return QIcon(pixmap)


def _tray_icon() -> QIcon:
    icon = QIcon.fromTheme("view-calendar-timeline", QIcon.fromTheme("folder-sync"))
    return icon if not icon.isNull() else _fallback_icon()


class ChronicleTray(QSystemTrayIcon):
    def __init__(self, sections) -> None:
        super().__init__(_tray_icon())
        self.sections = sections
        self.active = []

        menu = QMenu()
        for section in sections:
            ok, hint = section.available()
            if ok:
                section.build(menu)
                self.active.append(section)
            else:
                item = menu.addAction(hint)
                item.setEnabled(False)
            menu.addSeparator()

        backend_url = next(
            (
                s.backend_url()
                for s in self.active
                if hasattr(s, "backend_url") and s.backend_url()
            ),
            "",
        )
        if backend_url:
            menu.addAction(
                "Open Chronicle",
                lambda: QDesktopServices.openUrl(QUrl(backend_url)),
            )
        menu.addAction("View Logs", self.view_logs)
        menu.addAction("Quit tray", QApplication.quit)
        self.setContextMenu(menu)
        self.setToolTip("Chronicle")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def refresh(self) -> None:
        lines = ["Chronicle"]
        for section in self.active:
            try:
                section.refresh()
            except Exception:
                logger.exception("%s refresh failed", section.title)
            line = section.tooltip()
            if line:
                lines.append(line)
        self.setToolTip("\n".join(lines))

    def view_logs(self) -> None:
        dialog = QDialog()
        dialog.setWindowTitle("Chronicle Tray — Logs")
        dialog.resize(760, 440)
        layout = QVBoxLayout(dialog)
        lines = list(log_buffer.lines)
        layout.addWidget(QLabel(f"Last {len(lines)} application log line(s)"))
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setLineWrapMode(QPlainTextEdit.NoWrap)
        viewer.setPlainText("\n".join(lines) or "(no logs yet)")
        viewer.verticalScrollBar().setValue(viewer.verticalScrollBar().maximum())
        layout.addWidget(viewer)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def shutdown(self) -> None:
        for section in self.active:
            try:
                section.shutdown()
            except Exception:
                logger.exception("%s shutdown failed", section.title)


def run() -> None:
    configure_logging()

    if sys.platform == "darwin":
        # Menu-bar-only app: no Dock icon for a non-bundled Python process.
        try:
            from AppKit import NSApplication

            NSApplication.sharedApplication().setActivationPolicy_(1)
        except ImportError:
            pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise SystemExit("No system tray is available in this desktop session")

    tray = ChronicleTray([VaultSection(), ScreenPipeSection(), PendantSection()])
    tray.show()
    try:
        sys.exit(app.exec())
    finally:
        tray.shutdown()
