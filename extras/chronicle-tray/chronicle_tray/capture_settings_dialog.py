"""Capture settings dialog — one grid of what is recorded and what is sent.

Rows are capture sources, columns are the two independent axes: whether
ScreenPipe records the source on this machine and whether the Chronicle
collector forwards it. Forwarding a source requires recording it, so the
dialog keeps the "Send to Chronicle" column a subset of "Record locally" as
the user clicks. Screen frames have no forwarding switch in the collector —
whatever is recorded is forwarded — so that cell is a note rather than a box.

Nothing is written until the dialog is accepted: saving restarts the capture
services, so a live-applying menu made every intermediate click a restart.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
)

AUDIO_SOURCES = (("system", "System audio"), ("mic", "Microphone"))


class CaptureSettingsDialog(QDialog):
    """Edit capture/forwarding for every source; read the result on accept."""

    def __init__(
        self,
        captured: set[str],
        forwarded: set[str],
        screen_enabled: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chronicle — Capture Settings")
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Record locally</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Send to Chronicle</b>"), 0, 2)

        self.record_boxes: dict[str, QCheckBox] = {}
        self.forward_boxes: dict[str, QCheckBox] = {}
        for row, (source, title) in enumerate(AUDIO_SOURCES, start=1):
            grid.addWidget(QLabel(title), row, 0)
            record = QCheckBox()
            record.setChecked(source in captured)
            forward = QCheckBox()
            forward.setChecked(source in forwarded)
            forward.setEnabled(record.isChecked())
            record.toggled.connect(
                lambda checked, s=source: self._record_toggled(s, checked)
            )
            grid.addWidget(record, row, 1, Qt.AlignCenter)
            grid.addWidget(forward, row, 2, Qt.AlignCenter)
            self.record_boxes[source] = record
            self.forward_boxes[source] = forward

        screen_row = len(AUDIO_SOURCES) + 1
        grid.addWidget(QLabel("Screen"), screen_row, 0)
        self.screen_box = QCheckBox()
        self.screen_box.setChecked(screen_enabled)
        grid.addWidget(self.screen_box, screen_row, 1, Qt.AlignCenter)
        always = QLabel("always")
        always.setEnabled(False)
        grid.addWidget(always, screen_row, 2, Qt.AlignCenter)
        layout.addLayout(grid)

        note = QLabel(
            "Recorded screen frames are always sent to Chronicle. Audio can only "
            "be sent from a source this machine records."
        )
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _record_toggled(self, source: str, checked: bool) -> None:
        """A source that isn't recorded can't be forwarded — clear and lock it."""
        forward = self.forward_boxes[source]
        if not checked:
            forward.setChecked(False)
        forward.setEnabled(checked)

    def settings(self) -> tuple[set[str], set[str], bool]:
        """(recorded audio sources, forwarded audio sources, screen enabled)."""
        return (
            {s for s, box in self.record_boxes.items() if box.isChecked()},
            {s for s, box in self.forward_boxes.items() if box.isChecked()},
            self.screen_box.isChecked(),
        )
