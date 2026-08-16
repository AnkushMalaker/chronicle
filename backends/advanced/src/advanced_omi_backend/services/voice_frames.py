"""Bounded, non-authoritative low-latency PCM fan-out for active turns."""

import math

import redis.asyncio as redis

VOICE_FRAME_MAXLEN = 2_000
MIN_FRAME_DURATION_MS = 20
MAX_FRAME_DURATION_MS = 100


def voice_frame_stream(voice_session_id: str) -> str:
    if not voice_session_id:
        raise ValueError("voice_session_id is required")
    return f"voice:frames:{voice_session_id}"


class VoiceFramePublisher:
    """Publish native frames before the 250 ms durable producer accumulator."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def publish(
        self,
        *,
        voice_session_id: str,
        audio_session_id: str,
        capture_epoch: int,
        pcm: bytes,
        metadata: dict,
    ) -> str:
        if not audio_session_id:
            raise ValueError("audio_session_id is required")
        if capture_epoch < 0:
            raise ValueError("capture_epoch must be non-negative")
        if metadata.get("time_basis") != "captured":
            raise ValueError("interactive frame time_basis must be captured")
        frame_sequence = metadata.get("frame_sequence")
        if (
            not isinstance(frame_sequence, int)
            or isinstance(frame_sequence, bool)
            or frame_sequence < 0
        ):
            raise ValueError("frame_sequence must be a non-negative integer")
        monotonic_offset_ms = metadata.get("monotonic_offset_ms")
        if (
            not isinstance(monotonic_offset_ms, (int, float))
            or isinstance(monotonic_offset_ms, bool)
            or not math.isfinite(monotonic_offset_ms)
            or monotonic_offset_ms < 0
        ):
            raise ValueError("monotonic_offset_ms must be non-negative")
        captured_at_ms = metadata.get("captured_at_ms")
        if (
            not isinstance(captured_at_ms, (int, float))
            or isinstance(captured_at_ms, bool)
            or not math.isfinite(captured_at_ms)
            or captured_at_ms <= 0
        ):
            raise ValueError("captured_at_ms must be a positive timestamp")
        sample_rate = metadata.get("rate")
        channels = metadata.get("channels")
        sample_width = metadata.get("width")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (sample_rate, channels, sample_width)
        ):
            raise ValueError("frame audio format must contain positive integers")
        bytes_per_sample = channels * sample_width
        if not pcm or len(pcm) % bytes_per_sample:
            raise ValueError("frame PCM must be non-empty and sample-aligned")
        sample_count = len(pcm) // bytes_per_sample
        duration_ms = sample_count * 1000 / sample_rate
        if not MIN_FRAME_DURATION_MS <= duration_ms <= MAX_FRAME_DURATION_MS:
            raise ValueError(
                f"interactive frames must be {MIN_FRAME_DURATION_MS}-"
                f"{MAX_FRAME_DURATION_MS} ms, got {duration_ms:.3f} ms"
            )

        message_id = await self.redis.xadd(
            voice_frame_stream(voice_session_id),
            {
                "voice_session_id": voice_session_id,
                "audio_session_id": audio_session_id,
                "capture_epoch": str(capture_epoch),
                "frame_sequence": str(frame_sequence),
                "captured_at_ms": str(captured_at_ms),
                "monotonic_offset_ms": str(monotonic_offset_ms),
                "time_basis": "captured",
                "sample_rate": str(sample_rate),
                "channels": str(channels),
                "sample_width": str(sample_width),
                "sample_count": str(sample_count),
                "pcm": pcm,
            },
            maxlen=VOICE_FRAME_MAXLEN,
            approximate=False,
        )
        return message_id.decode() if isinstance(message_id, bytes) else message_id
