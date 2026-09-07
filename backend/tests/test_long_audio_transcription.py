import io
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from omegaconf import OmegaConf


def _wav_bytes(duration_seconds: float, sample_rate: int = 100) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * int(duration_seconds * sample_rate))
    return output.getvalue()


def _wav_duration(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as wav_file:
        return wav_file.getnframes() / wav_file.getframerate()


class _TenSecondProvider:
    name = "smallest"

    def __init__(self):
        self.durations = []

    def get_capabilities_dict(self):
        return {"word_timestamps": True, "diarization": True}

    async def transcribe(self, *, audio_data: bytes, **_kwargs):
        duration = _wav_duration(audio_data)
        self.durations.append(duration)
        if duration > 10.001:
            raise RuntimeError("Transcription service returned HTTP 408")

        call = len(self.durations)
        if call == 1:
            words = [
                {"word": "alpha", "start": 1.0, "end": 2.0},
                {"word": "shared-one", "start": 8.5, "end": 9.5},
            ]
        elif call == 2:
            words = [
                {"word": "shared-one", "start": 0.5, "end": 1.5},
                {"word": "middle", "start": 4.0, "end": 5.0},
                {"word": "shared-two", "start": 8.5, "end": 9.5},
            ]
        else:
            words = [
                {"word": "shared-two", "start": 0.5, "end": 1.5},
                {"word": "omega", "start": 4.0, "end": 5.0},
            ]
        return {
            "text": " ".join(word["word"] for word in words),
            "words": words,
            "segments": [
                {
                    "text": word["word"],
                    "start": word["start"],
                    "end": word["end"],
                    "speaker": "speaker_0",
                }
                for word in words
            ],
        }


@pytest.mark.asyncio
async def test_provider_ceiling_chunks_with_overlap_and_preserves_conversation_clock(
    monkeypatch,
):
    # Import locally so monkeypatches target the production entry-point module.
    from backend.workers import transcription_jobs as module

    provider = _TenSecondProvider()
    monkeypatch.setattr(
        module, "get_transcription_provider", lambda **_kwargs: provider
    )
    monkeypatch.setattr(
        module,
        "reconstruct_wav_from_conversation",
        AsyncMock(return_value=_wav_bytes(22.0)),
    )
    monkeypatch.setattr(
        module,
        "condense_silence",
        lambda pcm, *_args: (pcm, None, 22.0),
    )
    monkeypatch.setattr(
        module,
        "_align_result_words",
        AsyncMock(side_effect=lambda result, _wav: result),
    )
    monkeypatch.setattr(
        module,
        "get_batch_chunk_seconds",
        lambda provider_name: 10.0,
        raising=False,
    )
    monkeypatch.setattr(module, "BATCH_CHUNK_OVERLAP_SECONDS", 2.0, raising=False)

    result = await module.transcribe_audio_range(
        conversation_id="conversation",
        diarize=True,
    )

    assert provider.durations == pytest.approx([10.0, 10.0, 6.0])
    assert result["text"] == "alpha shared-one middle shared-two omega"
    assert [word["word"] for word in result["words"]] == [
        "alpha",
        "shared-one",
        "middle",
        "shared-two",
        "omega",
    ]
    assert [word["start"] for word in result["words"]] == pytest.approx(
        [1.0, 8.5, 12.0, 16.5, 20.0]
    )
    assert [segment["start"] for segment in result["segments"]] == pytest.approx(
        [1.0, 8.5, 12.0, 16.5, 20.0]
    )
    assert [segment["text"] for segment in result["segments"]] == [
        word["word"] for word in result["words"]
    ]
    assert all(
        segment["start"] <= word["start"] <= word["end"] <= segment["end"]
        for segment, word in zip(result["segments"], result["words"])
    )


@pytest.mark.asyncio
async def test_existing_full_audio_cache_wins_before_new_chunk_policy(monkeypatch):
    # Import locally so monkeypatches target the production entry-point module.
    from backend.workers import transcription_jobs as module

    cached = {
        "text": "cached transcript",
        "words": [{"word": "cached", "start": 1.0, "end": 2.0}],
        "segments": [
            {
                "text": "cached transcript",
                "start": 1.0,
                "end": 2.0,
                "speaker": "speaker_0",
            }
        ],
    }
    provider = SimpleNamespace(
        name="smallest",
        get_capabilities_dict=lambda: {"word_timestamps": True},
        get_cached_transcription=AsyncMock(return_value=cached),
        transcribe=AsyncMock(side_effect=AssertionError("paid provider must not run")),
    )
    monkeypatch.setattr(
        module, "get_transcription_provider", lambda **_kwargs: provider
    )
    monkeypatch.setattr(
        module,
        "reconstruct_wav_from_conversation",
        AsyncMock(return_value=_wav_bytes(22.0)),
    )
    monkeypatch.setattr(
        module,
        "condense_silence",
        lambda pcm, *_args: (pcm, None, 22.0),
    )
    monkeypatch.setattr(
        module,
        "_align_result_words",
        AsyncMock(side_effect=lambda result, _wav: result),
    )
    monkeypatch.setattr(
        module,
        "get_batch_chunk_seconds",
        lambda provider_name: 10.0,
        raising=False,
    )

    result = await module.transcribe_audio_range(conversation_id="conversation")

    assert result["text"] == "cached transcript"
    provider.get_cached_transcription.assert_awaited_once()
    provider.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_pyannote_mode_does_not_claim_or_request_provider_diarization(
    monkeypatch,
):
    # Import locally so monkeypatches target the production entry-point module.
    from backend.workers import transcription_jobs as module

    transcribe = AsyncMock(
        return_value={
            "text": "hello",
            "words": [{"word": "hello", "start": 0.1, "end": 0.5}],
            "segments": [{"text": "hello", "start": 0.1, "end": 0.5}],
        }
    )
    provider = SimpleNamespace(
        name="smallest",
        get_capabilities_dict=lambda: {
            "word_timestamps": True,
            "diarization": True,
        },
        transcribe=transcribe,
    )
    monkeypatch.setattr(
        module, "get_transcription_provider", lambda **_kwargs: provider
    )
    monkeypatch.setattr(
        module,
        "reconstruct_wav_from_conversation",
        AsyncMock(return_value=_wav_bytes(2.0)),
    )
    monkeypatch.setattr(
        module,
        "condense_silence",
        lambda pcm, *_args: (pcm, None, 2.0),
    )
    monkeypatch.setattr(
        module,
        "_align_result_words",
        AsyncMock(side_effect=lambda result, _wav: result),
    )
    monkeypatch.setattr(module, "get_batch_chunk_seconds", lambda _provider: 10.0)

    result = await module.transcribe_audio_range(
        conversation_id="conversation",
        diarize=False,
    )

    assert transcribe.await_args.kwargs["diarize"] is False
    assert result["provider_capabilities"] == {"word_timestamps": True}


def test_provider_chunk_ceiling_is_capped_by_global_limit(monkeypatch):
    # Import locally so the test can replace the live configuration authority.
    from backend import config as module

    monkeypatch.setattr(
        module,
        "get_backend_config",
        lambda _section: OmegaConf.create(
            {
                "batch_chunk_seconds": 3600,
                "batch_chunk_seconds_by_provider": {
                    "smallest": 540,
                    "misconfigured": 7200,
                },
            }
        ),
    )

    assert module.get_batch_chunk_seconds("smallest") == 540
    assert module.get_batch_chunk_seconds("misconfigured") == 3600
    assert module.get_batch_chunk_seconds("unlisted") == 3600


def test_shipped_smallest_chunk_ceiling_stays_outside_model_fingerprint():
    config_path = Path(__file__).resolve().parents[2] / "config" / "defaults.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert (
        config["backend"]["transcription"]["batch_chunk_seconds_by_provider"][
            "smallest"
        ]
        == 540
    )
    smallest = next(
        model for model in config["models"] if model["name"] == "stt-smallest"
    )
    assert "batch_chunk_seconds" not in smallest["operations"]["stt_transcribe"]
