from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from advanced_omi_backend.models.audio_capture import (
    AudioCaptureSession,
    CaptureEffects,
    CaptureEffectStatus,
)


def _capture_fields() -> dict:
    return {
        "capture_session_id": "capture-1",
        "user_id": "user-1",
        "capture_source_id": "phone-1",
        "client_id": "phone-1",
        "origin": "streaming",
        "time_basis": "received",
        "started_at": datetime(2026, 8, 16, tzinfo=timezone.utc),
    }


def test_capture_session_requires_complete_processing_provenance():
    with pytest.raises(ValidationError):
        AudioCaptureSession(**_capture_fields())


def test_reported_effects_require_requested_available_and_enabled_values():
    with pytest.raises(ValidationError):
        CaptureEffectStatus(reporting="reported")

    enabled = CaptureEffectStatus(
        reporting="reported", requested=True, available=True, enabled=True
    )
    assert enabled.enabled is True


def test_non_reported_effects_cannot_smuggle_boolean_claims():
    with pytest.raises(ValidationError):
        CaptureEffectStatus(
            reporting="unreported", requested=True, available=True, enabled=True
        )


def test_duplex_profile_requires_voice_session_and_reported_effects():
    with pytest.raises(ValidationError):
        AudioCaptureSession(
            **_capture_fields(),
            capture_epoch=3,
            processing_profile="duplex_aec",
            effects=CaptureEffects.unreported(),
            voice_session_id=None,
        )


def test_imported_profile_rejects_voice_session_binding():
    with pytest.raises(ValidationError):
        AudioCaptureSession(
            **_capture_fields(),
            capture_epoch=0,
            processing_profile="imported",
            effects=CaptureEffects.not_applicable(),
            voice_session_id="voice-1",
        )
