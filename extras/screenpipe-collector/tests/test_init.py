import importlib.util
from pathlib import Path

INIT_PATH = Path(__file__).parents[1] / "init.py"
SPEC = importlib.util.spec_from_file_location("screenpipe_collector_init", INIT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_screenpipe_unit_uses_privacy_defaults_and_api_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "SYSTEMD_USER_DIR", tmp_path)
    path = MODULE.write_screenpipe_unit(
        "/usr/bin/screenpipe",
        "local-key",
        "system",
        ["Speakers (output)"],
    )
    text = path.read_text()
    assert "--audio-transcription-engine disabled" in text
    assert "--disable-keyboard-capture" in text
    assert "--disable-clipboard-capture" in text
    assert "--api-auth true" in text
    assert "Environment=SCREENPIPE_API_KEY=local-key" in text
    assert "--use-system-default-audio false" in text
    assert "--audio-device 'Speakers (output)'" in text


def test_audio_arguments_keep_microphone_and_system_independent():
    devices = ["Mic (input)", "Speakers (output)"]

    assert MODULE.audio_arguments("system", devices)[-1] == "Speakers (output)"
    assert MODULE.audio_arguments("mic", devices)[-1] == "Mic (input)"
    assert MODULE.audio_arguments("both", devices) == [
        "--use-system-default-audio",
        "true",
    ]
