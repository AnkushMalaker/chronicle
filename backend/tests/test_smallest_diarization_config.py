from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _smallest_batch_model(config_path: Path) -> dict:
    config = yaml.safe_load(config_path.read_text())
    return next(model for model in config["models"] if model["name"] == "stt-smallest")


def test_smallest_batch_enables_advertised_diarization():
    model = _smallest_batch_model(ROOT / "config" / "defaults.yml")

    assert "diarization" in model["capabilities"]
    query = model["operations"]["stt_transcribe"]["query"]
    assert query["diarize"] == "true"


def test_smallest_template_enables_advertised_diarization():
    model = _smallest_batch_model(ROOT / "config" / "config.yml.template")

    assert "diarization" in model["capabilities"]
    query = model["operations"]["stt_transcribe"]["query"]
    assert query["diarize"] == "true"
