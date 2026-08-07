from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.encoders import jsonable_encoder

from advanced_omi_backend.models.timeline import TimelineEvidenceRef
from advanced_omi_backend.routers.modules.timeline_routes import (
    _episode_payload,
    _refs_overlapping,
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
        episode_key="key-one",
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="work",
        title="Work",
        summary="",
        status="active",
        confirmed_at=None,
        confirmed_fields=[],
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


def test_episode_payload_exposes_durable_identity_and_confirmation():
    """The UI needs these to show a confirmed badge and to survive reanalysis."""

    value = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        episode_id="episode-one",
        episode_key="durable-key",
        started_at=value,
        ended_at=value.replace(minute=45),
        kind="gaming_session",
        title="Played with Daksh",
        summary="",
        status="confirmed",
        confirmed_at=value,
        confirmed_fields=["title"],
        salience="notable",
        confidence=0.9,
        activity_mode="foreground",
        entities=["Daksh"],
        attributes={},
        assertions=[],
        evidence_refs=[
            TimelineEvidenceRef(
                evidence_id="audio_span:one",
                kind="audio_span",
                started_at=value,
                ended_at=value.replace(minute=45),
                role="uncertain",
                metadata={"conversation_id": "conv-42"},
            )
        ],
        related_episode_ids=[],
        related_conversation_ids=[],
        parent_episode_id=None,
        representative_image=None,
    )

    payload = jsonable_encoder(_episode_payload(episode))

    assert payload["episode_key"] == "durable-key"
    assert payload["status"] == "confirmed"
    assert payload["confirmed_fields"] == ["title"]
    # Without this the episode cannot deep-link into the recording it cites.
    assert payload["evidence"][0]["metadata"]["conversation_id"] == "conv-42"


def _ref(evidence_id: str, start_minute: int, end_minute: int | None):
    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return TimelineEvidenceRef(
        evidence_id=evidence_id,
        kind="observation",
        started_at=base + timedelta(minutes=start_minute),
        ended_at=None if end_minute is None else base + timedelta(minutes=end_minute),
        role="application_state",
    )


def test_split_repartitions_evidence_and_shares_only_spanning_refs():
    """Each half must cite what it actually covers.

    Copying every ref to both halves would make a split silently claim the same
    evidence twice; dropping a ref that straddles the cut would lose it entirely.
    """

    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        evidence_refs=[
            _ref("early", 0, 10),
            _ref("spanning", 5, 25),
            _ref("late", 20, 30),
            _ref("point-late", 22, None),
        ]
    )
    cut = base + timedelta(minutes=15)

    head = {ref.evidence_id for ref in _refs_overlapping(episode, base, cut)}
    tail = {
        ref.evidence_id
        for ref in _refs_overlapping(episode, cut, base + timedelta(minutes=30))
    }

    assert head == {"early", "spanning"}
    assert tail == {"spanning", "late", "point-late"}
    # Nothing is lost across the cut.
    assert head | tail == {"early", "spanning", "late", "point-late"}
