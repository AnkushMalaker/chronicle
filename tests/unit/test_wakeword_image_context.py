from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WAKEWORD = ROOT / "extras" / "wakeword-service"


def test_wakeword_image_includes_generated_audio_v2_contract():
    dockerfile = (WAKEWORD / "Dockerfile").read_text()
    dockerignore = (WAKEWORD / ".dockerignore").read_text().splitlines()

    assert "COPY audio_contract ./audio_contract" in dockerfile
    assert "!audio_contract/" in dockerignore
    assert "!audio_contract/**" in dockerignore
