"""KDE/Linux system tray for Chronicle capture and vault sync."""

import json
import logging
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSystemTrayIcon,
    QVBoxLayout,
)

from desktop_core import SharedState, VaultSyncManager, configure_logging, log_buffer

logger = logging.getLogger(__name__)
SCREENPIPE_DB = Path.home() / ".screenpipe/db.sqlite"
SCREENPIPE_UNIT = Path.home() / ".config/systemd/user/screenpipe.service"
COLLECTOR_CONFIG = Path.home() / ".config/chronicle-screenpipe/config.json"
load_dotenv(Path(__file__).resolve().parent / ".env")


def _unit_state(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name], capture_output=True, text=True
    )
    return result.stdout.strip() or "unknown"


def _screenpipe_stats() -> str:
    if not SCREENPIPE_DB.exists():
        return "No local database"
    try:
        uri = f"file:{SCREENPIPE_DB.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            frames = (
                db.execute("SELECT count(*) FROM frames").fetchone()[0]
                if "frames" in tables
                else 0
            )
            audio = (
                db.execute("SELECT count(*) FROM audio_chunks").fetchone()[0]
                if "audio_chunks" in tables
                else 0
            )
        size = sum(p.stat().st_size for p in SCREENPIPE_DB.parent.rglob("*") if p.is_file())
        return f"{frames:,} frames · {audio:,} audio chunks · {size / 1024**3:.1f} GiB"
    except (OSError, sqlite3.Error) as error:
        return f"Stats unavailable: {error}"


def _capture_settings(path: Path = SCREENPIPE_UNIT) -> tuple[str, bool]:
    """Return the audio mode and whether screen capture is enabled."""
    text = path.read_text(encoding="utf-8")
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    args = shlex.split(exec_start.removeprefix("ExecStart="))
    if "--disable-audio" in args:
        audio_mode = "off"
    else:
        devices = _argument_values(args, "--audio-device")
        follows_defaults = _argument_value(args, "--use-system-default-audio")
        if follows_defaults == "true" or (follows_defaults is None and not devices):
            return "both", "--disable-vision" not in args
        has_input = any(device.lower().endswith("(input)") for device in devices)
        has_output = any(device.lower().endswith("(output)") for device in devices)
        audio_mode = "both" if has_input and has_output else "mic" if has_input else "system"
    return audio_mode, "--disable-vision" not in args


def _argument_value(args: list[str], option: str) -> str | None:
    values = _argument_values(args, option)
    return values[-1] if values else None


def _argument_values(args: list[str], option: str) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == option]


def _without_options(args: list[str], options: set[str]) -> list[str]:
    result = []
    skip = False
    for value in args:
        if skip:
            skip = False
            continue
        if value in options:
            skip = True
            continue
        result.append(value)
    return result


def _audio_devices() -> list[str]:
    result = subprocess.run(
        ["screenpipe", "audio", "list", "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [entry["name"] for entry in json.loads(result.stdout)["data"]]


def _forward_audio_setting(path: Path = COLLECTOR_CONFIG) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))["forward_audio"]
    if value not in {"none", "output", "input", "both"}:
        raise ValueError(f"unsupported forwarding mode: {value}")
    return value


def _save_forward_audio_setting(mode: str, path: Path = COLLECTOR_CONFIG) -> None:
    if mode not in {"none", "output", "input", "both"}:
        raise ValueError(f"unsupported forwarding mode: {mode}")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["forward_audio"] = mode
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _audio_sources(mode: str, *, forwarding: bool = False) -> set[str]:
    """Expand a persisted audio mode into its enabled source names."""
    modes = (
        {
            "none": set(),
            "output": {"system"},
            "input": {"mic"},
            "both": {"system", "mic"},
        }
        if forwarding
        else {
            "off": set(),
            "system": {"system"},
            "mic": {"mic"},
            "both": {"system", "mic"},
        }
    )
    if mode not in modes:
        raise ValueError(f"unsupported audio mode: {mode}")
    return modes[mode]


def _audio_modes(captured: set[str], forwarded: set[str]) -> tuple[str, str]:
    """Collapse source sets into ScreenPipe and collector configuration values."""
    if not forwarded <= captured:
        raise ValueError("forwarded audio sources must also be recorded locally")
    capture_modes = {
        frozenset(): "off",
        frozenset({"system"}): "system",
        frozenset({"mic"}): "mic",
        frozenset({"system", "mic"}): "both",
    }
    forwarding_modes = {
        frozenset(): "none",
        frozenset({"system"}): "output",
        frozenset({"mic"}): "input",
        frozenset({"system", "mic"}): "both",
    }
    try:
        return capture_modes[frozenset(captured)], forwarding_modes[frozenset(forwarded)]
    except KeyError as error:
        raise ValueError(f"unsupported audio sources: {error.args[0]}") from error


def _updated_audio_modes(
    capture_mode: str,
    forwarding_mode: str,
    source: str,
    setting: str,
    enabled: bool,
) -> tuple[str, str]:
    """Apply one tray toggle while keeping forwarding dependent on capture."""
    if source not in {"system", "mic"}:
        raise ValueError(f"unsupported audio source: {source}")
    if setting not in {"record", "forward"}:
        raise ValueError(f"unsupported audio setting: {setting}")
    captured = _audio_sources(capture_mode)
    forwarded = _audio_sources(forwarding_mode, forwarding=True)
    target = captured if setting == "record" else forwarded
    if enabled:
        target.add(source)
        if setting == "forward":
            captured.add(source)
    else:
        target.discard(source)
        if setting == "record":
            forwarded.discard(source)
    return _audio_modes(captured, forwarded)


def _save_capture_settings(
    audio_mode: str,
    screen_enabled: bool,
    path: Path = SCREENPIPE_UNIT,
    audio_devices: list[str] | None = None,
) -> None:
    """Persist independent audio-source and screen settings in ScreenPipe's unit."""
    if audio_mode not in {"off", "system", "mic", "both"}:
        raise ValueError(f"unsupported audio mode: {audio_mode}")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
    args = shlex.split(lines[index].removeprefix("ExecStart="))
    args = _without_options(args, {"--audio-device", "--use-system-default-audio"})
    args = [arg for arg in args if arg not in {"--disable-audio", "--disable-vision"}]
    if audio_mode == "off":
        args.append("--disable-audio")
    elif audio_mode == "both":
        args.extend(["--use-system-default-audio", "true"])
    else:
        suffix = "(output)" if audio_mode == "system" else "(input)"
        matching = [
            name
            for name in (audio_devices or _audio_devices())
            if name.lower().endswith(suffix)
        ]
        if not matching:
            raise ValueError(f"no {audio_mode} audio device is available")
        args.extend(["--use-system-default-audio", "false", "--audio-device", matching[0]])
    if not screen_enabled:
        args.append("--disable-vision")
    lines[index] = f"ExecStart={shlex.join(args)}"
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")


class ChronicleTray(QSystemTrayIcon):
    def __init__(self, state: SharedState, manager: VaultSyncManager) -> None:
        icon = QIcon.fromTheme("view-calendar-timeline", QIcon.fromTheme("folder-sync"))
        super().__init__(icon)
        self.state = state
        self.manager = manager
        self.service_actions: dict[str, dict[str, QAction]] = {}
        self.pause_timer = QTimer(self)
        self.pause_timer.setSingleShot(True)
        self.pause_timer.timeout.connect(lambda: self.service("start", "screenpipe.service"))
        menu = QMenu()
        self.capture_status = menu.addAction("ScreenPipe: checking…")
        self.collector_status = menu.addAction("Chronicle collector: checking…")
        self.stats = menu.addAction("Stats: checking…")
        for item in (self.capture_status, self.collector_status, self.stats):
            item.setEnabled(False)
        menu.addSeparator()
        self._service_actions(menu, "ScreenPipe", "screenpipe.service", capture=True)
        self._service_actions(menu, "Collector", "chronicle-screenpipe.service")
        settings_menu = menu.addMenu("Settings")
        audio_menu = settings_menu.addMenu("Audio")
        self.audio_actions: dict[str, dict[str, QAction]] = {}
        for source, title in (("system", "System audio"), ("mic", "Microphone")):
            source_menu = audio_menu.addMenu(title)
            record = source_menu.addAction("Record locally")
            record.setCheckable(True)
            record.triggered.connect(
                lambda checked=False, selected=source: self.save_audio_source(
                    selected, "record", checked
                )
            )
            forward = source_menu.addAction("Send to Chronicle")
            forward.setCheckable(True)
            forward.triggered.connect(
                lambda checked=False, selected=source: self.save_audio_source(
                    selected, "forward", checked
                )
            )
            self.audio_actions[source] = {"record": record, "forward": forward}
        self.screen_capture = settings_menu.addAction("Screen capture")
        self.screen_capture.setCheckable(True)
        self.screen_capture.triggered.connect(self.save_capture_settings)
        menu.addSeparator()
        self.sync_status = menu.addAction("Vault sync: starting…")
        self.sync_status.setEnabled(False)
        menu.addAction("Open vault in Obsidian", self.open_obsidian)
        menu.addAction("Choose vault folder…", self.choose_folder)
        menu.addAction("Sync now / re-pair", manager.pair_async)
        menu.addSeparator()
        menu.addAction(
            "Open Chronicle",
            lambda: QDesktopServices.openUrl(QUrl(manager.config.backend_url)),
        )
        menu.addAction("View Logs", self.view_logs)
        menu.addAction("Quit tray", QApplication.quit)
        self.setContextMenu(menu)
        self.setToolTip("Chronicle")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def _service_actions(
        self, menu: QMenu, label: str, unit: str, capture: bool = False
    ) -> None:
        submenu = menu.addMenu(label)
        actions = {}
        for title, verb in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            action = QAction(title, submenu)
            action.triggered.connect(lambda _checked=False, v=verb, u=unit: self.service(v, u))
            submenu.addAction(action)
            actions[verb] = action
        self.service_actions[unit] = actions
        if capture:
            self.pause_menu = submenu.addMenu("Pause for")
            for title, minutes in (
                ("5 minutes", 5),
                ("15 minutes", 15),
                ("30 minutes", 30),
                ("1 hour", 60),
                ("2 hours", 120),
                ("8 hours", 480),
            ):
                self.pause_menu.addAction(
                    title, lambda _checked=False, m=minutes: self.pause_capture(m)
                )

    def service(self, verb: str, unit: str) -> None:
        if unit == "screenpipe.service" and verb in {"start", "restart"}:
            self.pause_timer.stop()
        subprocess.run(["systemctl", "--user", verb, unit], check=False)
        QTimer.singleShot(500, self.refresh)

    def pause_capture(self, minutes: int) -> None:
        self.service("stop", "screenpipe.service")
        self.pause_timer.start(minutes * 60 * 1000)

    def save_capture_settings(self, audio_mode: str | None = None) -> None:
        try:
            was_active = _unit_state("screenpipe.service") == "active"
            _save_capture_settings(
                audio_mode or _capture_settings()[0], self.screen_capture.isChecked()
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            if was_active:
                self.service("restart", "screenpipe.service")
        except (OSError, StopIteration, ValueError, subprocess.CalledProcessError) as error:
            logger.exception("Could not save ScreenPipe settings")
            self.showMessage("ScreenPipe settings", str(error), QSystemTrayIcon.Warning)
            self.refresh_capture_settings()

    def refresh_capture_settings(self) -> None:
        try:
            audio_mode, screen_enabled = _capture_settings()
            captured = _audio_sources(audio_mode)
            forwarded = _audio_sources(_forward_audio_setting(), forwarding=True)
            for source, actions in self.audio_actions.items():
                actions["record"].setChecked(source in captured)
                actions["forward"].setChecked(source in forwarded)
                actions["record"].setEnabled(True)
                actions["forward"].setEnabled(True)
            self.screen_capture.setChecked(screen_enabled)
            self.screen_capture.setEnabled(True)
        except (OSError, KeyError, StopIteration, ValueError, json.JSONDecodeError):
            logger.exception("Could not read audio settings")
            for actions in self.audio_actions.values():
                actions["record"].setEnabled(False)
                actions["forward"].setEnabled(False)
            self.screen_capture.setEnabled(False)

    def save_audio_source(self, source: str, setting: str, enabled: bool) -> None:
        try:
            was_capture_active = _unit_state("screenpipe.service") == "active"
            was_collector_active = _unit_state("chronicle-screenpipe.service") == "active"
            capture_mode, screen_enabled = _capture_settings()
            forwarding_mode = _forward_audio_setting()
            capture_mode, forwarding_mode = _updated_audio_modes(
                capture_mode, forwarding_mode, source, setting, enabled
            )
            _save_capture_settings(capture_mode, screen_enabled)
            _save_forward_audio_setting(forwarding_mode)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            if was_capture_active:
                self.service("restart", "screenpipe.service")
            if was_collector_active:
                self.service("restart", "chronicle-screenpipe.service")
        except (
            OSError,
            StopIteration,
            ValueError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ) as error:
            logger.exception("Could not save audio settings")
            self.showMessage("Audio settings", str(error), QSystemTrayIcon.Warning)
            self.refresh_capture_settings()

    def refresh(self) -> None:
        self.manager.refresh_status()
        capture = _unit_state("screenpipe.service")
        collector = _unit_state("chronicle-screenpipe.service")
        for unit, state in (
            ("screenpipe.service", capture),
            ("chronicle-screenpipe.service", collector),
        ):
            active = state == "active"
            self.service_actions[unit]["start"].setEnabled(not active)
            self.service_actions[unit]["stop"].setEnabled(active)
        self.pause_menu.setEnabled(capture == "active")
        self.refresh_capture_settings()
        self.capture_status.setText(f"ScreenPipe: {capture}")
        self.collector_status.setText(f"Chronicle collector: {collector}")
        self.stats.setText(_screenpipe_stats())
        snap = self.state.snapshot()
        if snap["status"] == "error":
            sync = f"error — {snap['error']}"
        elif snap["completion"] is not None:
            sync = f"{snap['completion']:.0f}%"
        else:
            sync = snap["status"]
        self.sync_status.setText(f"Vault sync: {sync}")
        self.setToolTip(f"Chronicle\nScreenPipe: {capture}\nCollector: {collector}\nVault: {sync}")

    def open_obsidian(self) -> None:
        vault = self.state.snapshot()["vault_dir"]
        Path(vault).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl(f"obsidian://open?path={quote(vault)}"))

    def choose_folder(self) -> None:
        current = self.state.snapshot()["vault_dir"]
        chosen = QFileDialog.getExistingDirectory(None, "Choose Chronicle vault", current)
        if chosen:
            self.manager.set_vault_dir(chosen)

    def view_logs(self) -> None:
        dialog = QDialog()
        dialog.setWindowTitle("Chronicle Desktop — Logs")
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


def main() -> None:
    configure_logging()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise SystemExit("No system tray is available in this desktop session")
    state = SharedState()
    manager = VaultSyncManager(state)
    manager.pair_async()
    tray = ChronicleTray(state, manager)
    tray.show()
    try:
        sys.exit(app.exec())
    finally:
        manager.shutdown()
