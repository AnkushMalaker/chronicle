"""
Audio utility functions for ASR services.

Provides common audio processing operations used across all providers.
"""

import io
import logging
import tempfile
import wave
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# Standard sample rate for ASR processing
STANDARD_SAMPLE_RATE = 16000


def convert_audio_to_numpy(
    audio_bytes: bytes,
    sample_rate: int,
    sample_width: int = 2,
    channels: int = 1,
) -> np.ndarray:
    """
    Convert raw audio bytes to numpy float32 array.

    Args:
        audio_bytes: Raw audio data
        sample_rate: Sample rate in Hz
        sample_width: Bytes per sample (2 = 16-bit, 4 = 32-bit)
        channels: Number of audio channels

    Returns:
        Numpy array of float32 audio samples normalized to [-1.0, 1.0]
    """
    if sample_width == 2:
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
        return audio_array.astype(np.float32) / np.iinfo(np.int16).max
    elif sample_width == 4:
        audio_array = np.frombuffer(audio_bytes, dtype=np.int32)
        return audio_array.astype(np.float32) / np.iinfo(np.int32).max
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")


def numpy_to_audio_bytes(audio_array: np.ndarray, sample_width: int = 2) -> bytes:
    """
    Convert numpy float32 array to raw audio bytes.

    Args:
        audio_array: Numpy array of float32 samples (normalized to [-1.0, 1.0])
        sample_width: Target bytes per sample (2 = 16-bit, 4 = 32-bit)

    Returns:
        Raw audio bytes
    """
    if sample_width == 2:
        audio_int = (audio_array * np.iinfo(np.int16).max).astype(np.int16)
        return audio_int.tobytes()
    elif sample_width == 4:
        audio_int = (audio_array * np.iinfo(np.int32).max).astype(np.int32)
        return audio_int.tobytes()
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")


def resample_audio(
    audio_array: np.ndarray, original_rate: int, target_rate: int = STANDARD_SAMPLE_RATE
) -> np.ndarray:
    """
    Resample audio to target sample rate using simple linear interpolation.

    For production use, consider using librosa or scipy for better quality.

    Args:
        audio_array: Input audio samples
        original_rate: Original sample rate in Hz
        target_rate: Target sample rate in Hz (default: 16000)

    Returns:
        Resampled audio array
    """
    if original_rate == target_rate:
        return audio_array

    # Calculate ratio and new length
    ratio = target_rate / original_rate
    new_length = int(len(audio_array) * ratio)

    # Simple linear interpolation resampling
    x_old = np.linspace(0, 1, len(audio_array))
    x_new = np.linspace(0, 1, new_length)
    resampled = np.interp(x_new, x_old, audio_array)

    return resampled.astype(np.float32)


def convert_to_mono(audio_array: np.ndarray, channels: int) -> np.ndarray:
    """
    Convert multi-channel audio to mono by averaging channels.

    Args:
        audio_array: Input audio samples (interleaved if multi-channel)
        channels: Number of channels in input

    Returns:
        Mono audio array
    """
    if channels == 1:
        return audio_array

    # Reshape to (samples, channels) and average
    audio_2d = audio_array.reshape(-1, channels)
    return audio_2d.mean(axis=1).astype(np.float32)


def load_audio_file(
    file_path: Union[str, Path], target_rate: int = STANDARD_SAMPLE_RATE
) -> Tuple[np.ndarray, int]:
    """
    Load audio file and return as numpy array.

    Args:
        file_path: Path to audio file (supports WAV)
        target_rate: Target sample rate (will resample if different)

    Returns:
        Tuple of (audio_array, sample_rate)
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    # Use wave module for WAV files
    with wave.open(str(file_path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        n_frames = wav_file.getnframes()

        audio_bytes = wav_file.readframes(n_frames)
        audio_array = convert_audio_to_numpy(
            audio_bytes, sample_rate, sample_width, channels
        )

        # Convert to mono if needed
        if channels > 1:
            audio_array = convert_to_mono(audio_array, channels)

        # Resample if needed
        if sample_rate != target_rate:
            logger.info(f"Resampling from {sample_rate}Hz to {target_rate}Hz")
            audio_array = resample_audio(audio_array, sample_rate, target_rate)
            sample_rate = target_rate

    return audio_array, sample_rate


def save_audio_file(
    audio_array: np.ndarray,
    file_path: Union[str, Path],
    sample_rate: int = STANDARD_SAMPLE_RATE,
    sample_width: int = 2,
) -> Path:
    """
    Save numpy audio array to WAV file.

    Args:
        audio_array: Audio samples as float32 normalized array
        file_path: Output file path
        sample_rate: Sample rate in Hz
        sample_width: Bytes per sample

    Returns:
        Path to saved file
    """
    file_path = Path(file_path)

    # Convert to bytes
    audio_bytes = numpy_to_audio_bytes(audio_array, sample_width)

    with wave.open(str(file_path), "wb") as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)

    return file_path


def save_to_temp_wav(
    audio_array: np.ndarray,
    sample_rate: int = STANDARD_SAMPLE_RATE,
    sample_width: int = 2,
    suffix: str = ".wav",
) -> str:
    """
    Save audio to a temporary WAV file.

    Args:
        audio_array: Audio samples as float32 normalized array
        sample_rate: Sample rate in Hz
        sample_width: Bytes per sample
        suffix: File suffix

    Returns:
        Path to temporary file (caller is responsible for cleanup)
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
        save_audio_file(audio_array, tmp_file.name, sample_rate, sample_width)
        return tmp_file.name


def get_audio_duration(
    audio_array: np.ndarray, sample_rate: int = STANDARD_SAMPLE_RATE
) -> float:
    """
    Calculate audio duration in seconds.

    Args:
        audio_array: Audio samples
        sample_rate: Sample rate in Hz

    Returns:
        Duration in seconds
    """
    return len(audio_array) / sample_rate


def validate_audio_format(sample_rate: int, channels: int, sample_width: int) -> None:
    """
    Validate audio format parameters.

    Args:
        sample_rate: Sample rate in Hz
        channels: Number of channels
        sample_width: Bytes per sample

    Raises:
        ValueError: If parameters are invalid
    """
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError(f"Sample rate {sample_rate} outside valid range (8000-48000)")
    if channels < 1 or channels > 8:
        raise ValueError(f"Channel count {channels} outside valid range (1-8)")
    if sample_width not in (2, 4):
        raise ValueError(f"Sample width {sample_width} must be 2 or 4 bytes")
