"""Normalize synthesized WAV audio into Chronicle V2 raw Opus packets."""

from __future__ import annotations

import audioop
import io
import wave
from dataclasses import dataclass

import opuslib

DOWNLINK_SAMPLE_RATE_HZ = 24_000
DOWNLINK_CHANNELS = 1
DOWNLINK_FRAME_MS = 20
DOWNLINK_FRAME_SAMPLES = DOWNLINK_SAMPLE_RATE_HZ * DOWNLINK_FRAME_MS // 1_000
DOWNLINK_FRAME_BYTES = DOWNLINK_FRAME_SAMPLES * 2
DOWNLINK_BITRATE_BPS = 24_000


@dataclass(frozen=True)
class EncodedPlayback:
    packets: tuple[bytes, ...]
    duration_ms: int


def encode_wav_for_playback(wav_body: bytes) -> EncodedPlayback:
    """Return 24 kHz mono, 20 ms raw Opus packets for one valid WAV body."""

    try:
        with wave.open(io.BytesIO(wav_body), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            pcm = reader.readframes(frame_count)
    except (EOFError, wave.Error) as error:
        raise ValueError("coordinated response must be a valid WAV") from error
    if channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
        raise ValueError("response WAV must be mono/stereo integer PCM")
    if sample_rate <= 0 or frame_count <= 0:
        raise ValueError("coordinated response WAV must contain audio frames")

    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
    if sample_width != 2:
        pcm = audioop.lin2lin(pcm, sample_width, 2)
    if sample_rate != DOWNLINK_SAMPLE_RATE_HZ:
        pcm, _state = audioop.ratecv(
            pcm, 2, DOWNLINK_CHANNELS, sample_rate, DOWNLINK_SAMPLE_RATE_HZ, None
        )

    encoder = opuslib.Encoder(DOWNLINK_SAMPLE_RATE_HZ, DOWNLINK_CHANNELS, "audio")
    encoder.bitrate = DOWNLINK_BITRATE_BPS
    packets: list[bytes] = []
    for offset in range(0, len(pcm), DOWNLINK_FRAME_BYTES):
        frame = pcm[offset : offset + DOWNLINK_FRAME_BYTES]
        if len(frame) < DOWNLINK_FRAME_BYTES:
            frame += bytes(DOWNLINK_FRAME_BYTES - len(frame))
        packets.append(encoder.encode(frame, DOWNLINK_FRAME_SAMPLES))
    return EncodedPlayback(
        packets=tuple(packets),
        duration_ms=max(1, round(frame_count * 1_000 / sample_rate)),
    )
