"""Audio processing utilities for speaker recognition system."""

import io
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import librosa
import numpy as np
import soundfile as sf


def load_audio(file_path: str, sr: int = 16000) -> Tuple[np.ndarray, int]:
    """
    Load audio file and return audio data and sample rate.

    Args:
        file_path: Path to audio file
        sr: Target sample rate (default: 16000 for speech)

    Returns:
        Tuple of (audio_data, sample_rate)
    """
    try:
        audio, sample_rate = librosa.load(file_path, sr=sr, mono=True)
        return audio, sample_rate
    except Exception as e:
        raise ValueError(f"Could not load audio file {file_path}: {str(e)}")


def load_audio_segment(
    file_path: str, start_time: float, end_time: float, sr: int = 16000
) -> Tuple[np.ndarray, int]:
    """
    Load a specific segment of an audio file.

    Args:
        file_path: Path to audio file
        start_time: Start time in seconds
        end_time: End time in seconds
        sr: Target sample rate

    Returns:
        Tuple of (audio_segment, sample_rate)
    """
    try:
        audio, sample_rate = librosa.load(
            file_path,
            sr=sr,
            mono=True,
            offset=start_time,
            duration=end_time - start_time,
        )
        return audio, sample_rate
    except Exception as e:
        raise ValueError(f"Could not load audio segment: {str(e)}")


def get_audio_info(file_path: str) -> Dict[str, Any]:
    """
    Get basic information about an audio file.

    Args:
        file_path: Path to audio file

    Returns:
        Dictionary with audio metadata
    """
    try:
        with sf.SoundFile(file_path) as f:
            duration = len(f) / f.samplerate
            return {
                "duration_seconds": duration,
                "sample_rate": f.samplerate,
                "channels": f.channels,
                "frames": len(f),
                "format": f.format,
                "subtype": f.subtype,
            }
    except Exception as e:
        # Fallback using librosa
        try:
            audio, sr = librosa.load(file_path, sr=None)
            return {
                "duration_seconds": len(audio) / sr,
                "sample_rate": sr,
                "channels": 1,  # librosa loads as mono by default
                "frames": len(audio),
                "format": "Unknown",
                "subtype": "Unknown",
            }
        except Exception as e2:
            raise ValueError(f"Could not get audio info: {str(e2)}")


def calculate_snr(
    audio: np.ndarray, frame_length: int = 2048, hop_length: int = 512
) -> float:
    """
    Calculate Signal-to-Noise Ratio of audio.

    Args:
        audio: Audio signal array
        frame_length: Frame length for analysis
        hop_length: Hop length for analysis

    Returns:
        SNR in decibels
    """
    try:
        # Calculate RMS energy
        rms = librosa.feature.rms(
            y=audio, frame_length=frame_length, hop_length=hop_length
        )[0]

        # Estimate noise floor (bottom 10th percentile)
        noise_floor = np.percentile(rms, 10)

        # Estimate signal level (top 90th percentile)
        signal_level = np.percentile(rms, 90)

        # Calculate SNR in dB
        if noise_floor > 0:
            snr_db = 20 * np.log10(signal_level / noise_floor)
        else:
            snr_db = 60.0  # Very high SNR if no noise detected

        return float(snr_db)
    except Exception:
        return 0.0


def detect_speech_segments(
    audio: np.ndarray, sr: int, min_duration: float = 0.5
) -> list:
    """
    Detect speech segments in audio using voice activity detection.

    Args:
        audio: Audio signal array
        sr: Sample rate
        min_duration: Minimum segment duration in seconds

    Returns:
        List of (start_time, end_time) tuples in seconds
    """
    try:
        # Use librosa's onset detection as a proxy for speech activity
        hop_length = 512
        frame_length = 2048

        # Calculate spectral centroid and RMS for VAD
        rms = librosa.feature.rms(
            y=audio, frame_length=frame_length, hop_length=hop_length
        )[0]

        # Threshold-based VAD (simple approach)
        threshold = np.percentile(rms, 30)  # Bottom 30% considered silence
        speech_frames = rms > threshold

        # Convert frame indices to time
        times = librosa.frames_to_time(
            np.arange(len(speech_frames)), sr=sr, hop_length=hop_length
        )

        # Find continuous speech segments
        segments = []
        start_time = None

        for i, is_speech in enumerate(speech_frames):
            if is_speech and start_time is None:
                start_time = times[i]
            elif not is_speech and start_time is not None:
                end_time = times[i]
                if end_time - start_time >= min_duration:
                    segments.append((start_time, end_time))
                start_time = None

        # Handle case where speech continues to end of audio
        if start_time is not None:
            end_time = times[-1]
            if end_time - start_time >= min_duration:
                segments.append((start_time, end_time))

        return segments
    except Exception:
        # Fallback: return entire audio as one segment
        return [(0.0, len(audio) / sr)]


def save_audio_segment(
    audio: np.ndarray, sr: int, output_path: str, format: str = "WAV"
):
    """
    Save audio segment to file.

    Args:
        audio: Audio signal array (converted to int16)
        sr: Sample rate
        output_path: Output file path
        format: Audio format (WAV, FLAC, etc.)
    """
    try:
        # Ensure int16 format for consistent file output
        if audio.dtype != np.int16:
            if audio.dtype in [np.float32, np.float64]:
                audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            else:
                audio = audio.astype(np.int16)

        sf.write(output_path, audio, sr, format=format, subtype="PCM_16")
    except Exception as e:
        raise ValueError(f"Could not save audio to {output_path}: {str(e)}")


def create_temp_audio_file(audio: np.ndarray, sr: int, suffix: str = ".wav") -> str:
    """
    Create a temporary audio file from audio data.

    Args:
        audio: Audio signal array (converted to int16)
        sr: Sample rate
        suffix: File extension

    Returns:
        Path to temporary file
    """
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    temp_file.close()

    # Ensure int16 format for consistent file output
    if audio.dtype != np.int16:
        if audio.dtype in [np.float32, np.float64]:
            audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        else:
            audio = audio.astype(np.int16)

    sf.write(temp_path, audio, sr, subtype="PCM_16")
    return temp_path


def audio_to_bytes(audio: np.ndarray, sr: int, format: str = "WAV") -> bytes:
    """
    Convert audio array to bytes for streaming/download.

    Args:
        audio: Audio signal array (should be int16 format)
        sr: Sample rate
        format: Audio format

    Returns:
        Audio data as bytes
    """
    # Ensure audio is int16 format for consistent output
    if audio.dtype != np.int16:
        if audio.dtype in [np.float32, np.float64]:
            # Convert from float (-1.0 to 1.0) to int16 (-32768 to 32767)
            audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        else:
            audio = audio.astype(np.int16)

    buffer = io.BytesIO()
    sf.write(buffer, audio, sr, format=format, subtype="PCM_16")
    buffer.seek(0)
    return buffer.read()


def normalize_audio(audio: np.ndarray, target_level: float = 0.5) -> np.ndarray:
    """
    Normalize audio to target level.

    Args:
        audio: Audio signal array
        target_level: Target RMS level (0-1)

    Returns:
        Normalized audio array
    """
    try:
        current_rms = np.sqrt(np.mean(audio**2))
        if current_rms > 0:
            scaling_factor = target_level / current_rms
            return audio * scaling_factor
        else:
            return audio
    except Exception:
        return audio


def concatenate_audio_segments(
    segments: list, sr: int, crossfade_ms: int = 50
) -> np.ndarray:
    """
    Concatenate multiple audio segments with optional crossfading.

    Args:
        segments: List of audio arrays
        sr: Sample rate
        crossfade_ms: Crossfade duration in milliseconds

    Returns:
        Concatenated audio array
    """
    if not segments:
        return np.array([])

    if len(segments) == 1:
        return segments[0]

    try:
        crossfade_samples = int(crossfade_ms * sr / 1000)
        result = segments[0].copy()

        for segment in segments[1:]:
            if (
                crossfade_samples > 0
                and len(result) >= crossfade_samples
                and len(segment) >= crossfade_samples
            ):
                # Apply crossfade
                fade_out = np.linspace(1, 0, crossfade_samples)
                fade_in = np.linspace(0, 1, crossfade_samples)

                result[-crossfade_samples:] *= fade_out
                segment_copy = segment.copy()
                segment_copy[:crossfade_samples] *= fade_in

                # Overlap and add
                result[-crossfade_samples:] += segment_copy[:crossfade_samples]
                result = np.concatenate([result, segment_copy[crossfade_samples:]])
            else:
                # Simple concatenation
                result = np.concatenate([result, segment])

        return result
    except Exception:
        # Fallback: simple concatenation
        return np.concatenate(segments)
