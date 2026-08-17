"""Contract tests for long-running ASR transcription progress streams."""

import io
import json
import wave
from collections.abc import Iterator

from common.base_service import BaseASRService, create_asr_app
from common.response_models import (
    TranscriptionProgressEvent,
    TranscriptionResult,
    TranscriptionResultEvent,
    TranscriptionStreamEvent,
)
from fastapi.testclient import TestClient


def _silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


class ProgressASRService(BaseASRService):
    @property
    def provider_name(self) -> str:
        return "progress-test"

    async def warmup(self) -> None:
        return None

    async def transcribe(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        raise AssertionError("the progress stream should handle this request")

    def get_capabilities(self) -> list[str]:
        return ["batch_progress"]

    def supports_batch_progress(self, audio_duration: float) -> bool:
        return True

    def transcribe_with_progress(
        self,
        audio_file_path: str,
        context_info: str | None = None,
        prompt: str | None = None,
    ) -> Iterator[TranscriptionStreamEvent]:
        yield TranscriptionProgressEvent(current=1, total=1)
        yield TranscriptionResultEvent.from_result(
            TranscriptionResult(text="hello", language="en", duration=0.01)
        )


def test_transcribe_stream_serializes_typed_progress_and_result_events():
    app = create_asr_app(ProgressASRService(model_id="progress-model"))

    with TestClient(app) as client:
        response = client.post(
            "/transcribe",
            files={"file": ("sample.wav", _silent_wav(), "audio/wav")},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [json.loads(line) for line in response.text.splitlines()] == [
        {"type": "progress", "current": 1, "total": 1},
        {
            "type": "result",
            "text": "hello",
            "words": [],
            "segments": [],
            "language": "en",
            "duration": 0.01,
        },
    ]
