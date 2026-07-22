import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from menu_linux import (
    _audio_modes,
    _audio_sources,
    _capture_settings,
    _forward_audio_setting,
    _save_capture_settings,
    _save_forward_audio_setting,
    _updated_audio_modes,
)


def _unit(tmp_path: Path, flags: str = "") -> Path:
    path = tmp_path / "screenpipe.service"
    path.write_text(
        "[Service]\nExecStart=/usr/bin/screenpipe record --api-auth true"
        f" {flags}\nRestart=on-failure\n",
        encoding="utf-8",
    )
    return path


def test_capture_settings_default_to_audio_and_screen_enabled(tmp_path):
    path = _unit(tmp_path)

    assert _capture_settings(path) == ("both", True)


def test_save_capture_settings_updates_only_capture_flags(tmp_path):
    path = _unit(tmp_path, "--disable-audio")

    _save_capture_settings(
        audio_mode="system",
        screen_enabled=False,
        path=path,
        audio_devices=["Speakers (output)"],
    )

    assert _capture_settings(path) == ("system", False)
    text = path.read_text(encoding="utf-8")
    assert "--api-auth true" in text
    assert "--disable-audio" not in text
    assert text.count("--disable-vision") == 1


def test_save_capture_settings_is_idempotent(tmp_path):
    path = _unit(tmp_path, "--disable-audio --disable-vision")

    _save_capture_settings(audio_mode="off", screen_enabled=False, path=path)

    text = path.read_text(encoding="utf-8")
    assert text.count("--disable-audio") == 1
    assert text.count("--disable-vision") == 1


def test_save_capture_settings_supports_both_default_sources(tmp_path):
    path = _unit(tmp_path, "--disable-audio")

    _save_capture_settings(audio_mode="both", screen_enabled=True, path=path)

    assert _capture_settings(path) == ("both", True)
    assert "--use-system-default-audio true" in path.read_text(encoding="utf-8")


def test_save_capture_settings_supports_microphone_only(tmp_path):
    path = _unit(tmp_path)

    _save_capture_settings(
        audio_mode="mic",
        screen_enabled=True,
        path=path,
        audio_devices=["Built-in Mic (input)", "Speakers (output)"],
    )

    assert _capture_settings(path) == ("mic", True)
    assert "--audio-device 'Built-in Mic (input)'" in path.read_text(encoding="utf-8")


def test_audio_forwarding_setting_is_independent_from_capture_unit(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"source_id": "rainbow", "forward_audio": "both"}')

    _save_forward_audio_setting("output", path)

    assert _forward_audio_setting(path) == "output"
    assert json.loads(path.read_text())["source_id"] == "rainbow"


def test_audio_source_modes_round_trip():
    assert _audio_sources("both") == {"system", "mic"}
    assert _audio_sources("input", forwarding=True) == {"mic"}
    assert _audio_modes({"system", "mic"}, {"system"}) == ("both", "output")


def test_forwarding_source_automatically_enables_local_recording():
    assert _updated_audio_modes("mic", "none", "system", "forward", True) == (
        "both",
        "output",
    )


def test_disabling_local_recording_also_disables_forwarding():
    assert _updated_audio_modes("both", "both", "system", "record", False) == (
        "mic",
        "input",
    )


def test_disabling_forwarding_keeps_local_recording():
    assert _updated_audio_modes("both", "both", "mic", "forward", False) == (
        "both",
        "output",
    )
