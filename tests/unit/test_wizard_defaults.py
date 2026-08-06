"""Test wizard.py helper functions for loading previous config as defaults.

Tests for the functions that read config/config.yml to pre-populate wizard
prompts with previously-configured values, so re-runs default to existing
settings.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import the pure helper functions directly from wizard.py.
# wizard.py lives at the project root, not inside a package, so we import
# via importlib with an explicit path to avoid adding the root to sys.path
# permanently.
# ---------------------------------------------------------------------------


WIZARD_PATH = Path(__file__).parent.parent.parent / "wizard.py"
PROJECT_ROOT = str(WIZARD_PATH.parent)


def _load_wizard():
    # wizard.py imports sibling root modules (discovery, services); add the root
    # so those resolve. chronicle_setup comes from the installed package.
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    spec = importlib.util.spec_from_file_location("wizard", WIZARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load once and reuse
_wizard = _load_wizard()

get_existing_stt_provider = _wizard.get_existing_stt_provider
get_existing_stream_provider = _wizard.get_existing_stream_provider
select_llm_provider = _wizard.select_llm_provider
select_setup_type = _wizard.select_setup_type
derive_langfuse_public_url = _wizard.derive_langfuse_public_url
infer_source_mode = _wizard._infer_source_mode


# ---------------------------------------------------------------------------
# get_existing_stt_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stt_value, expected",
    [
        ("stt-deepgram", "deepgram"),
        ("stt-deepgram-stream", "deepgram"),
        ("stt-parakeet-batch", "parakeet"),
        ("stt-vibevoice", "vibevoice"),
        ("stt-qwen3-asr", "qwen3-asr"),
        ("stt-smallest", "smallest"),
        ("stt-smallest-stream", "smallest"),
    ],
)
def test_get_existing_stt_provider_known_values(stt_value, expected):
    """Maps known config.yml stt values to wizard provider names."""
    config = {"defaults": {"stt": stt_value}}
    assert get_existing_stt_provider(config) == expected


def test_get_existing_stt_provider_unknown_returns_none():
    """Returns None for unknown stt values (e.g. custom providers)."""
    config = {"defaults": {"stt": "stt-unknown-provider"}}
    assert get_existing_stt_provider(config) is None


def test_get_existing_stt_provider_missing_key():
    """Returns None when defaults.stt key is absent."""
    assert get_existing_stt_provider({}) is None
    assert get_existing_stt_provider({"defaults": {}}) is None


# ---------------------------------------------------------------------------
# get_existing_stream_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stt_stream_value, expected",
    [
        ("stt-deepgram-stream", "deepgram"),
        ("stt-smallest-stream", "smallest"),
        ("stt-qwen3-asr", "qwen3-asr"),
        ("stt-qwen3-asr-stream", "qwen3-asr"),
    ],
)
def test_get_existing_stream_provider_known_values(stt_stream_value, expected):
    """Maps known config.yml stt_stream values to wizard streaming provider names."""
    config = {"defaults": {"stt_stream": stt_stream_value}}
    assert get_existing_stream_provider(config) == expected


def test_get_existing_stream_provider_unknown_returns_none():
    """Returns None for unknown stt_stream values."""
    config = {"defaults": {"stt_stream": "stt-unknown"}}
    assert get_existing_stream_provider(config) is None


def test_get_existing_stream_provider_missing_key():
    """Returns None when defaults.stt_stream is absent."""
    assert get_existing_stream_provider({}) is None
    assert get_existing_stream_provider({"defaults": {}}) is None


# ---------------------------------------------------------------------------
# select_llm_provider — test default resolution logic via EOFError path
# ---------------------------------------------------------------------------


def _select_llm_with_eof(config_yml):
    """Drive select_llm_provider in non-interactive mode by injecting EOFError."""
    with (
        patch.object(_wizard, "Confirm") as mock_confirm,
        patch.object(_wizard, "Prompt") as mock_prompt,
    ):
        mock_confirm.ask.side_effect = EOFError
        mock_prompt.ask.side_effect = EOFError
        return select_llm_provider(config_yml)


def test_select_llm_provider_defaults_to_openai_when_no_config():
    """Defaults to openai when config is empty."""
    result = _select_llm_with_eof({})
    assert result == "openai"


def test_select_llm_provider_defaults_to_openai_for_openai_llm():
    """Picks openai when existing config has defaults.llm = openai-llm."""
    config = {"defaults": {"llm": "openai-llm"}}
    result = _select_llm_with_eof(config)
    assert result == "openai"


def test_select_llm_provider_defaults_to_ollama_for_local_llm():
    """Picks ollama when existing config has defaults.llm = local-llm."""
    config = {"defaults": {"llm": "local-llm"}}
    result = _select_llm_with_eof(config)
    assert result == "ollama"


def test_select_llm_provider_treats_qwen_as_managed_llamacpp():
    """Qwen's concrete registry alias remains llama.cpp on wizard reruns."""
    config = {"defaults": {"llm": "qwen36-llm"}}
    result = _select_llm_with_eof(config)
    assert result == "llamacpp"


def test_local_llamacpp_uses_private_container_dns_on_rerun():
    assert _wizard.LOCAL_LLAMACPP_BASE_URL == "http://llama-cpp-llm:8080/v1"
    assert infer_source_mode(_wizard.LOCAL_LLAMACPP_BASE_URL) == "local"


def test_select_llm_provider_none_config():
    """Treats None config_yml as empty dict (defaults to openai)."""
    result = _select_llm_with_eof(None)
    assert result == "openai"


@pytest.mark.parametrize(
    "choice, expected", [("1", "main"), ("2", "join"), ("3", "capture")]
)
def test_select_setup_type(choice, expected):
    with patch.object(_wizard.Prompt, "ask", return_value=choice):
        assert select_setup_type() == expected


# ---------------------------------------------------------------------------
# Langfuse public browser URL
# ---------------------------------------------------------------------------


def test_local_langfuse_uses_caddy_https_when_https_is_enabled():
    assert (
        derive_langfuse_public_url("local", {}, "node.example.ts.net", True)
        == "https://node.example.ts.net:3443"
    )


def test_local_langfuse_uses_direct_http_without_https():
    assert (
        derive_langfuse_public_url("local", {}, "chronicle.local", False)
        == "http://chronicle.local:3002"
    )


def test_external_langfuse_keeps_its_configured_url():
    assert (
        derive_langfuse_public_url(
            "external", {"host": "https://cloud.langfuse.com"}, None, True
        )
        == "https://cloud.langfuse.com"
    )
