from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.timeline.review_projection import (
    build_day_review_projection,
)

BASE = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)


def episode(index: int, start: int, end: int, **changes):
    values = {
        "episode_id": f"episode-{index}",
        "episode_key": f"key-{index}",
        "run_id": "run-one",
        "user_id": "user-one",
        "local_date": date(2026, 8, 25),
        "timezone": "Asia/Kolkata",
        "started_at": BASE + timedelta(minutes=start),
        "ended_at": BASE + timedelta(minutes=end),
        "kind": "coding",
        "title": f"Episode {index}",
        "summary": "Summary",
        "confidence": 0.9,
        "activity_mode": "foreground",
        "evidence_refs": [SimpleNamespace(evidence_id=f"evidence-{index}")],
        "conversational": False,
        "audio_ranges": [],
        "entities": [],
        "salience": "routine",
        "confirmed_at": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_groups_temporally_adjacent_episodes_without_changing_episode_identity():
    items = [episode(1, 0, 10), episode(2, 20, 30), episode(3, 50, 60)]

    result = build_day_review_projection(
        items, local_date=date(2026, 8, 25), timezone_name="Asia/Kolkata"
    )

    assert result["episode_count"] == 3
    assert result["group_count"] == 2
    assert result["groups"][0]["episode_ids"] == ["episode-1", "episode-2"]
    assert result["groups"][1]["episode_ids"] == ["episode-3"]


def test_projection_surfaces_attention_and_conversation_context():
    item = episode(
        1,
        0,
        10,
        conversational=True,
        confidence=0.4,
        entities=["Amey"],
        evidence_refs=[],
    )

    result = build_day_review_projection(
        [item], local_date=date(2026, 8, 25), timezone_name="Asia/Kolkata"
    )
    group = result["groups"][0]

    assert group["lane"] == "conversation"
    assert group["title"] == "Conversation with Amey"
    assert group["needs_attention"] is True
    assert group["attention_reasons"] == [
        "low_confidence",
        "missing_audio",
        "missing_evidence",
    ]


def test_single_merged_conversation_uses_its_semantic_title_not_an_entity():
    item = episode(
        1,
        0,
        60,
        conversational=True,
        title="Interview about AI engineering and Neo",
        entities=["Amazon", "Ankush", "Aryan Neosapiens"],
        audio_ranges=[SimpleNamespace(range_id="audio-one")],
        confirmed_at=BASE,
    )

    result = build_day_review_projection(
        [item], local_date=date(2026, 8, 25), timezone_name="Asia/Kolkata"
    )

    assert result["groups"][0]["title"] == item.title


def test_groups_have_a_bounded_span_and_keep_visual_lanes_separate():
    items = [
        episode(1, 0, 70),
        episode(2, 80, 130),
        episode(3, 20, 40, activity_mode="background"),
    ]

    result = build_day_review_projection(
        items, local_date=date(2026, 8, 25), timezone_name="Asia/Kolkata"
    )

    assert result["group_count"] == 3
    assert [group["lane"] for group in result["groups"]] == [
        "foreground",
        "background",
        "foreground",
    ]


def test_semantic_group_keeps_separate_tape_intervals_and_reports_capture_duration():
    items = [episode(1, 0, 1), episode(2, 41, 42)]
    semantic_group = SimpleNamespace(
        group_id="group-one",
        episode_ids=["episode-1", "episode-2"],
        title="Voice ASR tests",
        summary="Two checks of the same live recording setup.",
    )

    result = build_day_review_projection(
        items,
        semantic_groups=[semantic_group],
        local_date=date(2026, 8, 25),
        timezone_name="Asia/Kolkata",
    )

    group = result["groups"][0]
    assert result["group_count"] == 1
    assert group["semantic"] is True
    assert group["intervals"] == [
        {
            "episode_id": "episode-1",
            "started_at": items[0].started_at,
            "ended_at": items[0].ended_at,
        },
        {
            "episode_id": "episode-2",
            "started_at": items[1].started_at,
            "ended_at": items[1].ended_at,
        },
    ]
    assert group["duration_seconds"] == 120
    assert group["span_seconds"] == 2520
    assert group["gap_seconds"] == 2400


def test_legacy_wide_episode_draws_its_discontiguous_audio_claims_not_the_envelope():
    item = episode(
        1,
        0,
        42,
        audio_ranges=[
            SimpleNamespace(
                started_at=BASE,
                ended_at=BASE + timedelta(seconds=11),
            ),
            SimpleNamespace(
                started_at=BASE + timedelta(minutes=41),
                ended_at=BASE + timedelta(minutes=41, seconds=29),
            ),
        ],
    )

    result = build_day_review_projection(
        [item], local_date=date(2026, 8, 25), timezone_name="Asia/Kolkata"
    )

    group = result["groups"][0]
    assert len(group["intervals"]) == 2
    assert group["duration_seconds"] == 40
    assert group["span_seconds"] == 2520
    assert group["gap_seconds"] == 2480
