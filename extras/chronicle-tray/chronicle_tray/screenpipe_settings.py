"""ScreenPipe capture/forwarding settings — pure helpers, no Qt.

Audio is controlled per source (system output, microphone) on two independent
axes: whether ScreenPipe records it locally (its systemd unit's ExecStart
flags) and whether the Chronicle collector forwards it (``forward_audio`` in
its config.json). Forwarding a source requires recording it, so the update
helper keeps forwarding a subset of capture.
"""

import json
import shlex
import subprocess
from pathlib import Path

SCREENPIPE_UNIT = Path.home() / ".config/systemd/user/screenpipe.service"
COLLECTOR_CONFIG = Path.home() / ".config/chronicle-screenpipe/config.json"


def _capture_settings(path: Path = SCREENPIPE_UNIT) -> tuple[str, bool]:
    """Return the audio mode and whether screen capture is enabled."""
    text = path.read_text(encoding="utf-8")
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
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
        audio_mode = (
            "both" if has_input and has_output else "mic" if has_input else "system"
        )
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
        return (
            capture_modes[frozenset(captured)],
            forwarding_modes[frozenset(forwarded)],
        )
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
        args.extend(
            ["--use-system-default-audio", "false", "--audio-device", matching[0]]
        )
    if not screen_enabled:
        args.append("--disable-vision")
    lines[index] = f"ExecStart={shlex.join(args)}"
    path.write_text(
        "\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8"
    )
