from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.encoders import jsonable_encoder

from advanced_omi_backend.models.timeline import TimelineEvidenceRef
from advanced_omi_backend.routers.modules.timeline_routes import (
    _episode_payload,
    _run_payload,
)


def assert_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_run_payload_marks_mongo_naive_datetimes_as_utc():
    value = datetime(2026, 8, 6, 12, 30)
    run = SimpleNamespace(
        run_id="run-one",
        state="complete",
        attempts=1,
        retry_after=None,
        error=None,
        evidence_revision="requested",
        processed_evidence_revision="processed",
        created_at=value,
        completed_at=value,
    )

    payload = jsonable_encoder(_run_payload(run))

    assert_utc(payload["created_at"])
    assert_utc(payload["completed_at"])


def test_episode_payload_marks_episode_and_evidence_datetimes_as_utc():
    value = datetime(2026, 8, 6, 12, 30)
    episode = SimpleNamespace(
        episode_id="episode-one",
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="work",
        title="Work",
        summary="",
        status="active",
        salience="routine",
        confidence=0.9,
        activity_mode="foreground",
        entities=[],
        attributes={},
        assertions=[],
        evidence_refs=[
            TimelineEvidenceRef(
                evidence_id="observation:one",
                kind="observation",
                started_at=value,
                ended_at=value.replace(minute=45),
                role="application_state",
            )
        ],
        related_episode_ids=[],
        related_conversation_ids=[],
        parent_episode_id=None,
        representative_image=None,
    )

    payload = jsonable_encoder(_episode_payload(episode))

    assert_utc(payload["started_at"])
    assert_utc(payload["ended_at"])
    assert_utc(payload["evidence"][0]["started_at"])
    assert_utc(payload["evidence"][0]["ended_at"])


def test_aware_payload_datetime_is_converted_to_utc():
    offset = timezone(timedelta(hours=5, minutes=30))
    run = SimpleNamespace(
        run_id="run-one",
        state="pending",
        attempts=0,
        retry_after=None,
        error=None,
        evidence_revision="requested",
        processed_evidence_revision=None,
        created_at=datetime(2026, 8, 6, 12, 30, tzinfo=offset),
        completed_at=None,
    )

    payload = jsonable_encoder(_run_payload(run))

    assert payload["created_at"] == "2026-08-06T07:00:00+00:00"
