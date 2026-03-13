"""Core utilities shared across API modules."""

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import HTTPException

log = logging.getLogger("speaker_service")


def get_data_directory() -> Path:
    """Get the appropriate data directory.

    Uses Docker path if available (/app/data), otherwise falls back to local development path.
    This pattern matches the database module's approach.
    """
    docker_path = Path("/app/data")
    if docker_path.exists():
        return docker_path
    else:
        # For local development, use 'data' directory relative to project root
        return Path("data")


def safe_format_confidence(confidence: Any, context: str = "") -> str:
    """Safely format confidence values with comprehensive validation and logging.

    Args:
        confidence: The confidence value to format (can be any type)
        context: Context string for logging (e.g., "speaker_identification", "audio_processing")

    Returns:
        str: Safely formatted confidence string (e.g., "0.856" or "invalid")
    """
    try:
        # Log the raw input for debugging
        log.debug(
            f"safe_format_confidence called with: type={type(confidence)}, value={confidence}, context='{context}'"
        )

        # Handle None values
        if confidence is None:
            log.debug(f"Confidence is None in context '{context}', returning '0.000'")
            return "0.000"

        # Convert to Python float, handling various input types
        if isinstance(confidence, (int, float)):
            conf_val = float(confidence)
        elif isinstance(confidence, np.number):
            # Handle numpy scalars (including float32, float64, etc.)
            conf_val = float(confidence.item())
            log.debug(f"Converted numpy {type(confidence)} to Python float: {conf_val}")
        elif hasattr(confidence, "__float__"):
            # Handle objects that can be converted to float
            conf_val = float(confidence)
            log.debug(
                f"Converted {type(confidence)} to float via __float__: {conf_val}"
            )
        else:
            log.warning(
                f"Cannot convert confidence type {type(confidence)} to float in context '{context}': {confidence}"
            )
            return "invalid"

        # Check for special float values
        if np.isnan(conf_val):
            log.warning(f"Confidence is NaN in context '{context}', returning '0.000'")
            return "0.000"
        elif np.isinf(conf_val):
            log.warning(
                f"Confidence is infinite ({conf_val}) in context '{context}', returning '1.000' or '0.000'"
            )
            return "1.000" if conf_val > 0 else "0.000"
        elif conf_val < 0:
            log.warning(
                f"Confidence is negative ({conf_val}) in context '{context}', clamping to 0.000"
            )
            return "0.000"
        elif conf_val > 1:
            log.warning(
                f"Confidence is > 1 ({conf_val}) in context '{context}', clamping to 1.000"
            )
            return "1.000"

        # Format the valid confidence value
        formatted = f"{conf_val:.3f}"
        log.debug(
            f"Successfully formatted confidence {conf_val} -> '{formatted}' in context '{context}'"
        )
        return formatted

    except Exception as e:
        log.error(
            f"Exception in safe_format_confidence for {confidence} (type: {type(confidence)}) in context '{context}': {e}"
        )
        return "error"


def secure_temp_file(suffix: str = ".wav") -> tempfile._TemporaryFileWrapper:
    """Create a secure temporary file."""
    return tempfile.NamedTemporaryFile(delete=False, suffix=suffix)


def extract_user_id_from_speaker_id(speaker_id: str) -> int:
    """Extract user_id from speaker_id format: user_{user_id}_..."""
    if not speaker_id.startswith("user_"):
        raise HTTPException(
            400,
            f"Invalid speaker_id format. Expected 'user_{{user_id}}_...', got: {speaker_id}",
        )

    try:
        parts = speaker_id.split("_")
        if len(parts) < 2:
            raise ValueError("Not enough parts")
        user_id = int(parts[1])
        return user_id
    except (ValueError, IndexError):
        raise HTTPException(
            400, f"Invalid speaker_id format. Cannot extract user_id from: {speaker_id}"
        )


def validate_confidence(confidence: Any, context: str = "") -> float:
    """Validate and sanitize confidence values from speaker identification.

    This is the simplified version used internally for validation logic.
    For formatted string output, use safe_format_confidence.

    Args:
        confidence: The confidence value to validate
        context: Context string for logging

    Returns:
        float: Valid confidence value between 0.0 and 1.0
    """
    if confidence is not None:
        if not isinstance(confidence, (int, float, np.number)):
            log.warning(
                f"Invalid confidence type from speaker_db.identify: {type(confidence)}, value: {confidence}"
            )
            confidence = 0.0
        elif np.isnan(confidence) or np.isinf(confidence):
            log.warning(
                f"Invalid confidence value from speaker_db.identify: {confidence} (NaN/Inf)"
            )
            confidence = 0.0
    else:
        confidence = 0.0

    # Ensure it's a float and clamp to valid range
    return max(0.0, min(1.0, float(confidence)))
