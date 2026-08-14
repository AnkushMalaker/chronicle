"""Request-level limits around bounded long-audio diarization."""

import asyncio
import io
import wave

from simple_speaker_recognition.api.routers.identification import (
    MAX_AUDIO_DURATION_SECONDS,
)
from simple_speaker_recognition.core.backend_client import BackendClient


def test_corpus_request_can_contain_many_bounded_ten_minute_passes():
    assert MAX_AUDIO_DURATION_SECONDS == 12 * 60 * 60


def test_twelve_hour_audio_fetch_has_a_bounded_fifteen_minute_read_timeout():
    client = BackendClient("http://chronicle.invalid")
    try:
        assert client.audio_timeout.read == 900.0
    finally:
        asyncio.run(client.close())


def _wav(sample: int, seconds: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(
            int(sample).to_bytes(2, "little", signed=True) * 16_000 * seconds
        )
    return output.getvalue()


def test_audio_timeline_preserves_gaps_as_silence(monkeypatch):
    """Missing chunks must not compress transcript timestamps before diarization."""

    client = BackendClient("http://chronicle.invalid")
    fetched = []

    async def get_audio_segment(_conversation_id, _token, start=0.0, duration=None):
        fetched.append((start, duration))
        return _wav(1000 if start == 0.0 else 2000)

    monkeypatch.setattr(client, "get_audio_segment", get_audio_segment)

    async def run():
        try:
            return await client.get_audio_timeline(
                "conversation",
                "token",
                total_duration=4.0,
                audio_ranges=[(0.0, 1.0), (3.0, 4.0)],
            )
        finally:
            await client.close()

    result = asyncio.run(run())

    with wave.open(io.BytesIO(result), "rb") as wav_file:
        assert wav_file.getnframes() == 4 * 16_000
        frames = wav_file.readframes(wav_file.getnframes())

    def sample_at(second: float) -> int:
        offset = int(second * 16_000) * 2
        return int.from_bytes(frames[offset : offset + 2], "little", signed=True)

    assert fetched == [(0.0, 1.0), (3.0, 1.0)]
    assert sample_at(0.5) == 1000
    assert sample_at(2.0) == 0
    assert sample_at(3.5) == 2000
