import argparse
import importlib.util
import sys
import types
from pathlib import Path


def _install_rich_stubs():
    rich_mod = types.ModuleType("rich")
    rich_console_mod = types.ModuleType("rich.console")
    rich_prompt_mod = types.ModuleType("rich.prompt")

    class _Console:
        def print(self, *args, **kwargs):
            return None

    class _Confirm:
        @staticmethod
        def ask(*args, **kwargs):
            return kwargs.get("default", False)

    class _Prompt:
        @staticmethod
        def ask(*args, **kwargs):
            return kwargs.get("default", "")

    rich_console_mod.Console = _Console
    rich_prompt_mod.Confirm = _Confirm
    rich_prompt_mod.Prompt = _Prompt

    sys.modules.setdefault("rich", rich_mod)
    sys.modules.setdefault("rich.console", rich_console_mod)
    sys.modules.setdefault("rich.prompt", rich_prompt_mod)


def _install_chronicle_setup_stub():
    mod = types.ModuleType("chronicle_setup")
    mod.ConfigManager = lambda *_, **__: None
    mod.detect_tailscale_info = lambda: (None, None)
    mod.decide_cert_mode = lambda *_: "caddy"
    mod.enable_tailscaled_at_boot = lambda *_: False
    mod.generate_tailscale_certs = lambda *_: False
    mod.is_placeholder = lambda value, *placeholders: value in placeholders
    mod.mask_value = lambda value, *_: value
    mod.prompt_password = lambda *_, **__: ""
    mod.prompt_with_existing_masked = (
        lambda *_, existing_value=None, default="", **__: existing_value or default
    )
    mod.read_env_value = lambda *_: None
    mod.tailscaled_enabled_at_boot = lambda *_: None
    mod.tailscale_socket_path = lambda *_: None
    sys.modules.setdefault("chronicle_setup", mod)


def _load_wizard_module():
    _install_rich_stubs()
    _install_chronicle_setup_stub()
    module_path = Path(__file__).resolve().parents[2] / "wizard.py"
    repo_root = str(module_path.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    spec = importlib.util.spec_from_file_location("wizard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


wizard = _load_wizard_module()


def _mock_read_env_value(path, key):
    if key == "PYTORCH_CUDA_VERSION":
        return "cu126"
    if key == "COMPUTE_MODE":
        return "cpu"
    return None


def test_run_service_setup_asr_uses_strix_provider_and_runtime(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd, check, timeout):
        captured["cmd"] = cmd
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(wizard, "check_service_exists", lambda *_: (True, "OK"))
    monkeypatch.setattr(wizard, "read_env_value", _mock_read_env_value)
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    ok = wizard.run_service_setup(
        service_name="asr-services",
        selected_services=["backend", "asr-services"],
        transcription_provider="parakeet",
        hardware_profile="strixhalo",
    )

    assert ok is True
    cmd = captured["cmd"]
    assert "--provider" in cmd
    provider_idx = cmd.index("--provider") + 1
    assert cmd[provider_idx] == "nemo-strixhalo"
    assert cmd[provider_idx] != "nemo"
    assert "--pytorch-cuda-version" in cmd
    runtime_idx = cmd.index("--pytorch-cuda-version") + 1
    assert cmd[runtime_idx] == "strixhalo"
    assert cmd[runtime_idx] != "cu126"


def test_run_service_setup_asr_vibevoice_uses_strix_variant(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd, check, timeout):
        captured["cmd"] = cmd
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(wizard, "check_service_exists", lambda *_: (True, "OK"))
    monkeypatch.setattr(wizard, "read_env_value", _mock_read_env_value)
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    ok = wizard.run_service_setup(
        service_name="asr-services",
        selected_services=["backend", "asr-services"],
        transcription_provider="vibevoice",
        hardware_profile="strixhalo",
    )

    assert ok is True
    cmd = captured["cmd"]
    assert "--provider" in cmd
    provider_idx = cmd.index("--provider") + 1
    assert cmd[provider_idx] == "vibevoice-strixhalo"
    assert cmd[provider_idx] != "vibevoice"
    assert "--pytorch-cuda-version" in cmd
    runtime_idx = cmd.index("--pytorch-cuda-version") + 1
    assert cmd[runtime_idx] == "strixhalo"
    assert cmd[runtime_idx] != "cu126"


def test_run_service_setup_speaker_forces_strix_compute(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd, check, timeout):
        captured["cmd"] = cmd
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(wizard, "check_service_exists", lambda *_: (True, "OK"))
    monkeypatch.setattr(wizard, "read_env_value", _mock_read_env_value)
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    ok = wizard.run_service_setup(
        service_name="speaker-recognition",
        selected_services=["backend", "speaker-recognition"],
        hf_token="hf_token",
        hardware_profile="strixhalo",
    )

    assert ok is True
    cmd = captured["cmd"]
    assert "--pytorch-cuda-version" in cmd
    runtime_idx = cmd.index("--pytorch-cuda-version") + 1
    assert cmd[runtime_idx] == "strixhalo"
    compute_pairs = [
        cmd[i + 1] for i, v in enumerate(cmd[:-1]) if v == "--compute-mode"
    ]
    assert "gpu" in compute_pairs
    assert "cpu" not in compute_pairs


def test_select_hardware_profile_returns_strix(monkeypatch):
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *args, **kwargs: "2")

    result = wizard.select_hardware_profile(
        selected_services=["speaker-recognition"],
        transcription_provider="deepgram",
        streaming_provider=None,
    )

    assert result == "strixhalo"


def test_select_hardware_profile_skips_when_not_needed(monkeypatch):
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *args, **kwargs: "2")

    result = wizard.select_hardware_profile(
        selected_services=["backend"],
        transcription_provider="deepgram",
        streaming_provider=None,
    )

    assert result is None
