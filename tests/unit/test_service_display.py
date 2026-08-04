"""Displayed label and ports for services whose compose hosts many providers.

``asr-services`` is one compose for every local ASR provider, so its static
SERVICES entry names only whichever provider was hardcoded. Everything that
shows a service to a human — ``status.py``, Tailnet advertising, the node
agent feeding the WebUI System page — resolves both through these helpers so
the displayed name and ports cannot drift from what is actually configured.
"""

from pathlib import Path

import services


def _install_asr_env(monkeypatch, tmp_path: Path, env: str, stt_stream: str = ""):
    """Point services.py at a throwaway tree holding an asr-services .env."""
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    (tmp_path / "extras" / "asr-services").mkdir(parents=True)
    (tmp_path / "extras" / "asr-services" / ".env").write_text(env)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text(
        f"defaults:\n  stt_stream: {stt_stream}\n"
    )


def test_label_resolves_the_configured_asr_provider(monkeypatch, tmp_path):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\n")

    assert services.service_display_label("asr-services") == "VibeVoice ASR"


def test_label_falls_back_to_the_static_description_for_an_unset_provider(
    monkeypatch, tmp_path
):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=\n")

    assert services.service_display_label("asr-services") == (
        services.SERVICES["asr-services"]["description"]
    )


def test_label_is_the_static_description_for_single_provider_services(
    monkeypatch, tmp_path
):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\n")

    assert services.service_display_label("backend") == (
        services.SERVICES["backend"]["description"]
    )


def test_ports_use_the_configured_asr_port(monkeypatch, tmp_path):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\nASR_PORT=9001\n")

    assert services.service_display_ports("asr-services") == ["9001"]


def test_nemotron_reports_its_stream_port_not_asr_port(monkeypatch, tmp_path):
    # Nemotron serves batch from the streaming container, so ASR_PORT is not
    # where it listens — showing 8767 would point at nothing.
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=nemotron\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
    )

    assert services.service_display_ports("asr-services") == ["8772"]


def test_a_cloud_only_selection_reports_no_ports(monkeypatch, tmp_path):
    # No local container runs, so a port here would be a phantom. The WebUI
    # relies on this being empty rather than a stale default.
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=deepgram\n")

    assert services.service_display_ports("asr-services") == []


def test_an_active_streaming_lane_adds_its_own_port(monkeypatch, tmp_path):
    # A streaming provider can run beside a different batch provider, so both
    # containers are up and both ports belong in the display.
    streaming = services.STREAMING_ASR_PROVIDER_OPTIONS["nemotron"]
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=vibevoice\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
        stt_stream=str(streaming["model"]),
    )

    assert services.service_display_ports("asr-services") == ["8767", "8772"]


def test_a_shared_port_is_not_listed_twice(monkeypatch, tmp_path):
    # Nemotron as both batch and streaming resolves to one container on one
    # port; the two lanes must collapse rather than render ":8772, :8772".
    streaming = services.STREAMING_ASR_PROVIDER_OPTIONS["nemotron"]
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=nemotron\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
        stt_stream=str(streaming["model"]),
    )

    assert services.service_display_ports("asr-services") == ["8772"]
