"""
Windowed batch transcription consumer.

Used when no streaming ASR is configured. Buffers ~N seconds of streamed audio per
session and transcribes each window with the configured batch STT provider, writing
results to transcription:results:{session_id} exactly like the streaming consumer — so
speech detection and the conversation lifecycle keep working unchanged.

Without this, a continuous/static source is only transcribed when its WebSocket
disconnects (BaseAudioStreamConsumer is otherwise abstract and never instantiated).
"""

import logging

import redis.asyncio as redis

from backend.config import require_speech_for_transcription
from backend.services.audio_stream.consumer import BaseAudioStreamConsumer
from backend.services.transcription import get_transcription_provider
from backend.utils.audio_utils import pcm_to_wav_bytes
from backend.utils.vad_analysis import detect_speech_pcm

logger = logging.getLogger(__name__)

# AudioStreamProducer emits fixed 0.25s PCM chunks; the buffer flush is chunk-count
# based, so window_seconds maps to a chunk count via this constant.
CHUNK_SECONDS = 0.25


class WindowedBatchConsumer(BaseAudioStreamConsumer):
    """Batch-transcribes fixed-duration windows of streamed audio."""

    def __init__(self, redis_client: redis.Redis, window_seconds: float = 30.0):
        buffer_chunks = max(1, round(window_seconds / CHUNK_SECONDS))
        super().__init__(
            provider_name="windowed-batch",
            redis_client=redis_client,
            buffer_chunks=buffer_chunks,
        )
        self.window_seconds = window_seconds

        self._provider = get_transcription_provider(mode="batch")
        if self._provider is None:
            raise RuntimeError(
                "No batch STT provider configured (defaults.stt). "
                "Windowed batch segmentation requires a batch transcription provider."
            )

        logger.info(
            f"WindowedBatchConsumer: window={window_seconds:.0f}s "
            f"({buffer_chunks} chunks), provider={self._provider.name}"
        )

    async def transcribe_audio(self, audio_data: bytes, sample_rate: int) -> dict:
        """Transcribe one buffered window with the batch provider.

        Wraps raw PCM as WAV so every batch provider (Deepgram raw-audio, Parakeet /
        gemma4 multipart file upload) receives well-formed audio.
        """
        # Audio-filtering gate: a silent window otherwise costs a full provider
        # call. Only a definitive no-speech result rejects; unscored audio fails open.
        if require_speech_for_transcription():
            speech_detection = detect_speech_pcm(audio_data, sample_rate, 1, 2)
            if speech_detection.should_reject:
                logger.debug(
                    "🔇 No speech in %.0fs window — skipping batch call "
                    "(reason=%s, scored=%s)",
                    self.window_seconds,
                    speech_detection.reason.value,
                    speech_detection.scored,
                )
                return {"text": "", "words": [], "segments": [], "confidence": 0.0}
        wav_bytes = pcm_to_wav_bytes(audio_data, sample_rate=sample_rate)
        result = await self._provider.transcribe(
            audio_data=wav_bytes, sample_rate=sample_rate, diarize=False
        )
        return {
            "text": result.get("text", ""),
            "words": result.get("words", []),
            "segments": result.get("segments", []),
            "confidence": 0.0,
        }
