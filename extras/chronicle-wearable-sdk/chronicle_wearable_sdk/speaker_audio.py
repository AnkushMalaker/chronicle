"""Convert Chronicle WAV responses to Elato's bounded Opus packet stream."""

from __future__ import annotations

import io
import wave

import numpy as np
import opuslib

ELATO_SPEAKER_RATE = 24_000
ELATO_SPEAKER_CHANNELS = 1
ELATO_FRAME_MS = 60
ELATO_FRAME_SAMPLES = ELATO_SPEAKER_RATE * ELATO_FRAME_MS // 1000


def _read_pcm16_mono(wav: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(wav), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (EOFError, wave.Error) as error:
        raise ValueError("speaker response must be a valid WAV") from error
    if channels <= 0 or sample_rate <= 0 or sample_width != 2:
        raise ValueError("speaker response must be 16-bit PCM WAV")
    samples = np.frombuffer(frames, dtype="<i2")
    if len(samples) % channels:
        raise ValueError("speaker response has an incomplete PCM frame")
    matrix = samples.reshape((-1, channels))
    mono = matrix.astype(np.int32).mean(axis=1).round().astype(np.int16)
    return mono, sample_rate


def _resample_linear(samples: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == ELATO_SPEAKER_RATE or len(samples) == 0:
        return samples
    output_length = max(1, round(len(samples) * ELATO_SPEAKER_RATE / source_rate))
    source_positions = np.arange(len(samples), dtype=np.float64)
    output_positions = np.arange(output_length, dtype=np.float64) * (
        source_rate / ELATO_SPEAKER_RATE
    )
    resampled = np.interp(
        output_positions, source_positions, samples.astype(np.float64)
    )
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def encode_wav_to_opus_packets(wav: bytes) -> list[bytes]:
    """Return 60 ms, 24 kHz mono Opus packets for one WAV response."""

    samples, sample_rate = _read_pcm16_mono(wav)
    samples = _resample_linear(samples, sample_rate)
    if len(samples) == 0:
        raise ValueError("speaker response contains no PCM frames")
    encoder = opuslib.Encoder(
        ELATO_SPEAKER_RATE,
        ELATO_SPEAKER_CHANNELS,
        opuslib.APPLICATION_VOIP,
    )
    packets: list[bytes] = []
    for offset in range(0, len(samples), ELATO_FRAME_SAMPLES):
        frame = samples[offset : offset + ELATO_FRAME_SAMPLES]
        if len(frame) < ELATO_FRAME_SAMPLES:
            frame = np.pad(frame, (0, ELATO_FRAME_SAMPLES - len(frame)))
        packets.append(
            encoder.encode(frame.astype("<i2").tobytes(), ELATO_FRAME_SAMPLES)
        )
    return packets
