import pytest
from bson import ObjectId

from advanced_omi_backend.models.audio_capture import CaptureEffects
from scripts.backfill_capture_provenance import plan_document, validate_provenance

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("origin", "profile", "reporting"),
    [
        ("streaming", "ambient", "unreported"),
        ("upload", "imported", "not_applicable"),
        ("batch", "imported", "not_applicable"),
        ("import", "imported", "not_applicable"),
        ("screenpipe", "source_native", "unreported"),
    ],
)
def test_historical_origin_maps_to_one_strict_provenance(origin, profile, reporting):
    plan = plan_document(
        {"_id": ObjectId(), "capture_session_id": "capture-1", "origin": origin}
    )

    assert plan is not None
    assert plan.updates["capture_epoch"] == 0
    assert plan.updates["processing_profile"] == profile
    assert plan.updates["effects"]["aec"]["reporting"] == reporting
    assert plan.updates["voice_session_id"] is None


def test_conflicting_partial_provenance_is_refused():
    with pytest.raises(ValueError, match="conflicting partial provenance"):
        plan_document(
            {
                "_id": ObjectId(),
                "capture_session_id": "capture-1",
                "origin": "streaming",
                "capture_epoch": 9,
            }
        )


def test_complete_interactive_provenance_is_valid_and_not_rewritten():
    document = {
        "_id": ObjectId(),
        "capture_session_id": "capture-1",
        "origin": "streaming",
        "capture_epoch": 3,
        "processing_profile": "duplex_aec",
        "effects": {
            "aec": {
                "reporting": "reported",
                "requested": True,
                "available": True,
                "enabled": True,
            },
            "noise_suppression": {
                "reporting": "reported",
                "requested": True,
                "available": True,
                "enabled": True,
            },
        },
        "voice_session_id": "voice-1",
    }

    assert plan_document(document) is None
    validate_provenance(document)


def test_imported_provenance_rejects_unreported_effects():
    with pytest.raises(ValueError, match="imported provenance"):
        validate_provenance(
            {
                "capture_epoch": 0,
                "processing_profile": "imported",
                "effects": CaptureEffects.unreported().model_dump(mode="json"),
                "voice_session_id": None,
            }
        )
