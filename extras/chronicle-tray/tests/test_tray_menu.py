"""The tray shell's own menu: logging buffer and the View Logs entry.

Qt needs a platform plugin even to build a menu, so these run offscreen.
"""

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chronicle_tray.logs import configure_logging, log_buffer


def test_log_buffer_keeps_formatted_lines():
    configure_logging()
    log_buffer.lines.clear()

    logging.getLogger("chronicle_tray.test").info("sync stalled")

    assert any("sync stalled" in line for line in log_buffer.lines)


def test_configure_logging_does_not_add_the_buffer_twice():
    configure_logging()
    configure_logging()

    assert logging.getLogger().handlers.count(log_buffer) == 1


def test_log_buffer_is_bounded():
    from chronicle_tray.logs import MemoryLogHandler

    handler = MemoryLogHandler(capacity=3)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for index in range(5):
        handler.emit(
            logging.LogRecord(
                "t", logging.INFO, __file__, index, str(index), None, None
            )
        )

    assert list(handler.lines) == ["2", "3", "4"]


def test_screenpipe_menu_offers_capture_toggles_and_a_settings_dialog():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from chronicle_tray.sections.screenpipe import IS_LINUX, ScreenPipeSection

    if not IS_LINUX:
        pytest.skip("capture settings are edited through systemd units")

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    section = ScreenPipeSection()
    menu = QtWidgets.QMenu()
    section.build(menu)
    try:
        # QAction.menu() hands ownership of the submenu to the action wrapper:
        # chaining off a temporary action collects the submenu with it.
        capture_action = next(
            action for action in menu.actions() if action.text() == "Capture"
        )
        capture = capture_action.menu()
        assert [item.text() for item in capture.actions()] == [
            "Audio capture",
            "Video capture",
        ]
        assert all(item.isCheckable() for item in capture.actions())
        assert "Capture settings…" in [action.text() for action in menu.actions()]
    finally:
        section.shutdown()
        del menu


def test_tray_menu_offers_view_logs():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from chronicle_tray.app import ChronicleTray

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tray = ChronicleTray([])
    try:
        labels = [action.text() for action in tray.contextMenu().actions()]
        assert "View Logs" in labels
    finally:
        tray.shutdown()
        del tray
        app.processEvents()
