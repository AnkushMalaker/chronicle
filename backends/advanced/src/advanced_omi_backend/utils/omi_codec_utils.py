"""Helpers for OMI Opus payload metadata handling."""

from typing import Any


def is_opus_header_stripped(audio_start_data: dict[str, Any] | None) -> bool:
    """
    Determine whether incoming OMI Opus payloads already have BLE header removed.

    Defaults to True because current mobile and relay clients send header-stripped
    payload bytes. Raw BLE packet sources can override with
    ``opus_header_stripped: false``.
    """
    if not audio_start_data:
        return True

    value = audio_start_data.get("opus_header_stripped", True)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off"}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True

    return bool(value)
