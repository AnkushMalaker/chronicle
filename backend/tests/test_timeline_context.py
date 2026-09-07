from datetime import date, datetime, timedelta, timezone

from backend.models.timeline import EvidenceLocator
from backend.services.timeline.context import (
    TimelineContextEvent,
    TimelineContextSummary,
    build_context_blocks,
    compact_evidence,
    condenser_context_payload,
    final_context_payload,
    is_dense_context_block,
    parse_context_response,
    passthrough_context_summary,
    repair_context_summary,
)
from backend.services.timeline.contracts import (
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)


def _manifest(items):
    started = datetime(2026, 8, 6, tzinfo=timezone.utc)
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=started,
        ended_at=started + timedelta(days=1),
        evidence_revision="revision",
        windows=[],
        evidence=items,
    )


def _item(index, *, kind="observation", excerpt="screen text"):
    started = datetime(2026, 8, 6, tzinfo=timezone.utc) + timedelta(minutes=index)
    return TimelineEvidenceItem(
        evidence_id=f"evidence-{index}",
        kind=kind,
        locator=EvidenceLocator(
            capture_source_id=(
                "conversation" if kind == "transcript" else "screenpipe-default"
            ),
            modality="transcript" if kind == "transcript" else "screen",
            track_id="input" if kind == "transcript" else "display-1",
        ),
        started_at=started,
        ended_at=started + timedelta(seconds=30),
        role="uncertain" if kind == "transcript" else "application_state",
        excerpt=excerpt,
        metadata={"conversation_id": "conversation"} if kind == "transcript" else {},
    )


def test_model_cannot_supply_evidence_accounting():
    block = build_context_blocks(_manifest([_item(1)]))[0]
    payload = passthrough_context_summary(block).model_dump(mode="json")
    payload["events"][0]["coverage"] = {
        "source_count": 0,
        "retained_count": 0,
        "agent_visible_count": 0,
        "cited_count": 7,
    }
    summary = parse_context_response(payload)
    repaired, _ = repair_context_summary(block, summary)
    assert repaired.events[0].coverage.cited_count == 0
    assert repaired.events[0].evidence_ids == ["evidence-1"]


def test_context_blocks_assign_every_source_item_once_without_window_duplication():
    items = [_item(index, excerpt="x" * 600) for index in range(12)]

    blocks = build_context_blocks(_manifest(items), max_chars=2500, max_items=5)

    ids = [
        evidence_id
        for block in blocks
        for item in block["evidence"]
        for evidence_id in item["evidence_ids"]
    ]
    assert ids == [item.evidence_id for item in items]
    assert len(ids) == len(set(ids))
    assert len(blocks) > 1


def test_compaction_keeps_more_dialogue_than_noisy_screen_text():
    transcript = compact_evidence(_item(1, kind="transcript", excerpt="t" * 7000))
    screen = compact_evidence(_item(2, excerpt="s" * 7000))

    assert len(transcript["excerpt"]) == 5000
    assert len(screen["excerpt"]) == 800


def test_dense_blocks_are_selected_for_local_agent_condensation():
    block = build_context_blocks(_manifest([_item(i) for i in range(4)]))[0]

    assert is_dense_context_block(block, min_items=4, min_chars=1_000_000)
    assert not is_dense_context_block(block, min_items=5, min_chars=1_000_000)


def test_condenser_cannot_drop_or_invent_original_evidence_ids():
    block = build_context_blocks(_manifest([_item(1), _item(2)]))[0]
    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=block["started_at"],
                ended_at=block["ended_at"],
                summary="Combined activity",
                evidence_ids=["evidence-1", "invented"],
            )
        ]
    )

    repaired, warnings = repair_context_summary(block, summary)

    cited = [item for event in repaired.events for item in event.evidence_ids]
    assert set(cited) == {"evidence-1", "evidence-2"}
    assert len(cited) == len(set(cited))
    assert all("invented" not in event.evidence_ids for event in repaired.events)
    assert any("unknown" in warning for warning in warnings)
    assert any("expanded 1" in warning for warning in warnings)


def test_condenser_payload_uses_representative_ids_and_repair_restores_groups_once():
    items = [_item(index) for index in (0, 1, 2, 3, 4, 7)]
    block = build_context_blocks(_manifest(items))[0]

    payload = condenser_context_payload(block)

    first_group = payload["evidence"][0]
    assert first_group["source_evidence_count"] == 5
    assert first_group["evidence_ids"] == ["evidence-0", "evidence-4"]

    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=block["started_at"],
                ended_at=block["ended_at"],
                summary="Representative screen transition",
                evidence_ids=["evidence-0"],
            )
        ]
    )
    repaired, warnings = repair_context_summary(block, summary)

    cited = [item for event in repaired.events for item in event.evidence_ids]
    assert cited == [
        "evidence-0",
        "evidence-1",
        "evidence-2",
        "evidence-3",
        "evidence-4",
        "evidence-7",
    ]
    assert len(repaired.events) == 2
    assert any("expanded 4" in warning for warning in warnings)
    assert any("restored 1" in warning for warning in warnings)


def test_missing_context_groups_are_restored_in_bounded_chronological_bundles():
    items = [
        _item(index, kind="transcript", excerpt=f"utterance {index}")
        for index in range(100)
    ]
    block = build_context_blocks(_manifest(items))[0]
    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=items[0].started_at,
                ended_at=items[0].ended_at,
                summary="First utterance",
                evidence_ids=[items[0].evidence_id],
            )
        ]
    )

    repaired, warnings = repair_context_summary(block, summary)

    cited = [item for event in repaired.events for item in event.evidence_ids]
    assert cited == [item.evidence_id for item in items]
    assert len(cited) == len(set(cited))
    assert len(repaired.events) == 9
    assert repaired.events[1].summary.startswith("00:01:00–00:01:30 utterance 1")
    assert "00:13:00–00:13:30 utterance 13" in repaired.events[1].summary
    assert any("restored 99" in warning for warning in warnings)


def test_fallback_bundles_never_bridge_large_uncaptured_gaps():
    items = [
        *[
            _item(index, kind="transcript", excerpt=f"early {index}")
            for index in range(10)
        ],
        *[
            _item(index, kind="transcript", excerpt=f"late {index}")
            for index in range(600, 609)
        ],
    ]
    block = build_context_blocks(_manifest(items))[0]
    alternating = [
        value
        for pair in zip(items[:9], items[10:])
        for value in (pair[0].evidence_id, pair[1].evidence_id)
    ]
    alternating.append(items[9].evidence_id)
    summary = TimelineContextSummary(
        events=[],
        # A model may return unresolved IDs out of order. Repair still owns chronology.
        unresolved_evidence_ids=alternating,
    )

    repaired, _ = repair_context_summary(block, summary)

    assert repaired.events == sorted(
        repaired.events, key=lambda event: (event.started_at, event.ended_at)
    )
    assert all(
        event.ended_at - event.started_at < timedelta(hours=1)
        for event in repaired.events
    )


def test_condenser_events_are_split_across_large_uncaptured_gaps():
    early = _item(1, kind="transcript", excerpt="early transcript")
    late = _item(36, kind="transcript", excerpt="late transcript")
    block = build_context_blocks(_manifest([early, late]))[0]
    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=early.started_at,
                ended_at=late.ended_at,
                summary="The condenser incorrectly joined both transcript islands",
                evidence_ids=[early.evidence_id, late.evidence_id],
            )
        ]
    )

    repaired, warnings = repair_context_summary(block, summary)

    assert [event.evidence_ids for event in repaired.events] == [
        [early.evidence_id],
        [late.evidence_id],
    ]
    assert [(event.started_at, event.ended_at) for event in repaired.events] == [
        (early.started_at, early.ended_at),
        (late.started_at, late.ended_at),
    ]
    assert any(
        "split" in warning and "evidence islands" in warning for warning in warnings
    )


def test_final_context_keeps_boundary_citations_without_dumping_every_source_id():
    items = [_item(index, kind="transcript") for index in range(20)]
    for item in items:
        item.metadata["meeting_id"] = "meeting:recorder:21"
    block = build_context_blocks(_manifest(items))[0]
    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=items[0].started_at,
                ended_at=items[-1].ended_at,
                summary="One bounded meeting summary",
                evidence_ids=[item.evidence_id for item in items],
            )
        ]
    )

    payload = final_context_payload(block, summary, condensed=True)
    event = payload["events"][0]

    assert event["source_evidence_count"] == 20
    assert event["meeting_ids"] == ["meeting:recorder:21"]
    assert len(event["evidence_ids"]) == 6
    assert event["evidence_ids"][:3] == ["evidence-0", "evidence-1", "evidence-2"]
    assert event["evidence_ids"][-3:] == [
        "evidence-17",
        "evidence-18",
        "evidence-19",
    ]


def test_final_context_separates_mixed_meeting_and_post_meeting_transport():
    meeting = _item(0, kind="transcript", excerpt="Rishon wraps up the meeting")
    meeting.metadata["meeting_id"] = "meeting:recorder:21"
    slack = _item(1, excerpt="Slack DM after the call")
    photo = _item(2, kind="immich", excerpt="Photo captured after the call")
    block = build_context_blocks(_manifest([meeting, slack, photo]))[0]
    summary = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=meeting.started_at,
                ended_at=photo.ended_at,
                summary="Meeting conclusion, followed by Slack and a photo",
                evidence_ids=[
                    meeting.evidence_id,
                    slack.evidence_id,
                    photo.evidence_id,
                ],
            )
        ]
    )

    payload = final_context_payload(block, summary, condensed=True)

    assert len(payload["events"]) == 2
    meeting_event = next(event for event in payload["events"] if event["meeting_ids"])
    post_meeting_event = next(
        event for event in payload["events"] if not event["meeting_ids"]
    )
    assert meeting_event["evidence_ids"] == [meeting.evidence_id]
    assert meeting_event["summary"].endswith("Rishon wraps up the meeting")
    assert post_meeting_event["evidence_ids"] == [slack.evidence_id, photo.evidence_id]
    assert "Slack DM after the call" in post_meeting_event["summary"]
    assert "Photo captured after the call" in post_meeting_event["summary"]
    assert "Rishon" not in post_meeting_event["summary"]


def test_final_context_bounds_legacy_cached_passthrough_event_explosion():
    items = [
        _item(index, kind="transcript", excerpt=f"utterance {index}")
        for index in range(100)
    ]
    block = build_context_blocks(_manifest(items))[0]
    legacy_cached_summary = passthrough_context_summary(block)

    payload = final_context_payload(block, legacy_cached_summary, condensed=True)

    assert len(payload["events"]) <= 16
    assert sum(event["source_evidence_count"] for event in payload["events"]) == 100
    assert payload["events"][0]["summary"].startswith("00:00:00–00:00:30 utterance 0")
    assert "00:05:00–00:05:30 utterance 5" in payload["events"][0]["summary"]


def test_final_context_rebundling_preserves_large_temporal_discontinuities():
    items = [
        *[_item(index, kind="transcript") for index in range(6)],
        *[_item(index, kind="transcript") for index in range(600, 613)],
    ]
    block = build_context_blocks(_manifest(items))[0]

    payload = final_context_payload(
        block, passthrough_context_summary(block), condensed=True
    )

    assert all(
        datetime.fromisoformat(event["ended_at"].replace("Z", "+00:00"))
        - datetime.fromisoformat(event["started_at"].replace("Z", "+00:00"))
        < timedelta(hours=1)
        for event in payload["events"]
    )
