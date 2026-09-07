"""Tests for the OmegaConf-backed configuration loader."""

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from backend.config import get_config, reload_config


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    return tmp_path


def test_get_config_merges_defaults_and_user_overrides(config_dir: Path):
    OmegaConf.save(
        {
            "defaults": {"llm": "openai-llm", "stt": "stt-deepgram"},
            "backend": {"cleanup": {"enabled": False, "retention_days": 30}},
        },
        config_dir / "defaults.yml",
    )
    OmegaConf.save(
        {
            "defaults": {"llm": "local-llm"},
            "backend": {"cleanup": {"retention_days": 14}},
        },
        config_dir / "config.yml",
    )

    config = get_config(force_reload=True)

    assert config["defaults"] == {"llm": "local-llm", "stt": "stt-deepgram"}
    assert config["backend"]["cleanup"] == {
        "enabled": False,
        "retention_days": 14,
    }


def test_get_config_merges_models_by_name(config_dir: Path):
    OmegaConf.save(
        {
            "models": [
                {"name": "default-llm", "model_type": "llm"},
                {"name": "default-stt", "model_type": "stt"},
            ]
        },
        config_dir / "defaults.yml",
    )
    OmegaConf.save(
        {
            "models": [
                {
                    "name": "default-llm",
                    "model_type": "llm",
                    "model_name": "custom-model",
                }
            ]
        },
        config_dir / "config.yml",
    )

    config = get_config(force_reload=True)

    assert config["models"] == [
        {
            "name": "default-llm",
            "model_type": "llm",
            "model_name": "custom-model",
        },
        {"name": "default-stt", "model_type": "stt"},
    ]


def test_reload_config_reads_changes_from_disk(config_dir: Path):
    config_path = config_dir / "config.yml"
    OmegaConf.save({"defaults": {"llm": "openai-llm"}}, config_path)
    assert get_config(force_reload=True)["defaults"]["llm"] == "openai-llm"

    OmegaConf.save({"defaults": {"llm": "local-llm"}}, config_path)
    reload_config()

    assert get_config()["defaults"]["llm"] == "local-llm"


def test_get_config_supports_absolute_config_file(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    alternate_config = tmp_path / "alternate.yml"
    OmegaConf.save({"defaults": {"stt": "stt-smallest"}}, alternate_config)
    monkeypatch.setenv("CONFIG_FILE", str(alternate_config))

    config = get_config(force_reload=True)

    assert config["defaults"]["stt"] == "stt-smallest"
