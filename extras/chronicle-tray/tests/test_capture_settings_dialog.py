"""The capture settings dialog keeps forwarding a subset of local recording.

Qt needs a platform plugin even to build widgets, so these run offscreen.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def dialog():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from chronicle_tray.capture_settings_dialog import CaptureSettingsDialog

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = CaptureSettingsDialog({"system", "mic"}, {"system"}, screen_enabled=True)
    yield widget
    widget.deleteLater()


def test_dialog_opens_on_the_current_settings(dialog):
    assert dialog.settings() == ({"system", "mic"}, {"system"}, True)
    assert dialog.forward_boxes["mic"].isEnabled()


def test_disabling_local_recording_also_disables_forwarding(dialog):
    dialog.record_boxes["system"].setChecked(False)

    assert dialog.settings() == ({"mic"}, set(), True)
    assert not dialog.forward_boxes["system"].isEnabled()


def test_re_enabling_local_recording_leaves_forwarding_off_but_available(dialog):
    dialog.record_boxes["system"].setChecked(False)
    dialog.record_boxes["system"].setChecked(True)

    assert dialog.settings() == ({"system", "mic"}, set(), True)
    assert dialog.forward_boxes["system"].isEnabled()


def test_forwarding_a_recorded_source_can_be_switched_on(dialog):
    dialog.forward_boxes["mic"].setChecked(True)

    assert dialog.settings() == ({"system", "mic"}, {"system", "mic"}, True)


def test_screen_capture_is_recorded_only(dialog):
    dialog.screen_box.setChecked(False)

    assert dialog.settings()[2] is False
    assert "screen" not in dialog.forward_boxes
