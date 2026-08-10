from datetime import date, datetime, timedelta, timezone

import pytest

from advanced_omi_backend.services.timeline.codex_executor import (
    CodexTimelineExecutor,
    _parse_usage,
    _workspace_fingerprint,
)
from advanced_omi_backend.services.timeline.contracts import (
    AgentAssertion,
    AgentAttribute,
    AgentEpisode,
    TimelineAgentResult,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    UnassignedInterval,
)
from advanced_omi_backend.services.timeline.executor import (
    TimelineIncompleteSegmentation,
    validate_agent_result,
)


def _manifest() -> TimelineEvidenceManifest:
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    item = TimelineEvidenceItem(
        evidence_id="observation:one",
        kind="observation",
        started_at=start + timedelta(minutes=2),
        ended_at=start + timedelta(minutes=25),
        role="application_state",
        excerpt="Terminator playback",
    )
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=start,
        ended_at=end,
        evidence_revision="revision",
        windows=[
            TimelineCoverageWindow(
                window_id="window:one",
                started_at=start,
                ended_at=end,
                evidence_ids=[item.evidence_id],
            )
        ],
        evidence=[item],
    )


def _result(evidence_id: str = "observation:one") -> TimelineAgentResult:
    manifest = _manifest()
    return TimelineAgentResult(
        episodes=[
            AgentEpisode(
                kind="media_watched",
                title="Watched Terminator",
                summary="A long movie playback session.",
                started_at=manifest.started_at + timedelta(minutes=2),
                ended_at=manifest.started_at + timedelta(minutes=25),
                salience="routine",
                activity_mode="background",
                confidence=0.9,
                attributes=[AgentAttribute(key="media", value="Terminator")],
                evidence_ids=[evidence_id],
                assertions=[
                    AgentAssertion(
                        claim="Terminator was playing",
                        role="application_state",
                        confidence=0.9,
                        evidence_ids=[evidence_id],
                    )
                ],
            )
        ],
    )


def test_workspace_fingerprint_tracks_inputs_but_ignores_generated_outputs(tmp_path):
    (tmp_path / "evidence.json").write_text("first", encoding="utf-8")
    (tmp_path / "timeline-result.json").write_text("generated", encoding="utf-8")

    first = _workspace_fingerprint(tmp_path)
    (tmp_path / "evidence.json").write_text("second", encoding="utf-8")
    second = _workspace_fingerprint(tmp_path)

    assert [entry["path"] for entry in first] == ["evidence.json"]
    assert first != second


@pytest.mark.asyncio
async def test_cached_timeline_result_bypasses_quota_and_provider(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cached_result = _result().model_dump(mode="json")

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.codex_executor.load_reusable_result",
        lambda operation, request: cached_result,
    )
    executor = CodexTimelineExecutor({})
    monkeypatch.setattr(
        executor,
        "_check_quota",
        lambda: pytest.fail("cache hit must happen before quota or provider checks"),
    )

    result = await executor.analyze(workspace, _manifest(), [])

    assert result.episodes[0].title == "Watched Terminator"
    assert result.usage["cache_hits"] == 1


def test_valid_overlapping_episode_result_is_accepted():
    validate_agent_result(_result(), _manifest())


def test_a_day_whose_only_episode_is_malformed_fails():
    """Dropping bad episodes must not quietly turn a broken run into an empty day."""

    with pytest.raises(TimelineIncompleteSegmentation, match="unknown evidence"):
        validate_agent_result(_result("observation:invented"), _manifest())


def test_saying_nothing_about_a_day_with_evidence_is_a_failure_not_an_answer():
    """Regression: this was tolerated, and every empty run silently blanked a day.

    Materializing the gap as "unassigned" made a model failure look like a successful
    analysis, so the day published zero episodes and superseded a good generation.
    """

    result = TimelineAgentResult(episodes=[], unassigned_intervals=[])

    with pytest.raises(TimelineIncompleteSegmentation):
        validate_agent_result(result, _manifest())


def test_evidence_the_agent_partially_explained_is_still_materialized():
    """The materialize behaviour is right once the agent has said *something*."""

    manifest = _manifest()
    result = _result()
    # Cited evidence spans 2-25min; the episode only covers 2-10min.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=10)

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals
    assert result.unassigned_intervals[-1].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_unavailable_representative_image_is_dropped_without_losing_episode():
    result = _result()
    result.episodes[0].representative_evidence_id = "observation:one"
    validate_agent_result(result, _manifest())
    assert result.episodes[0].representative_evidence_id is None


def test_non_overlapping_citation_is_pruned_when_episode_remains_grounded():
    manifest = _manifest()
    outside = TimelineEvidenceItem(
        evidence_id="observation:outside",
        kind="observation",
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=28),
        role="application_state",
        excerpt="Later application state",
    )
    manifest.evidence.append(outside)
    result = _result()
    result.episodes[0].evidence_ids.append(outside.evidence_id)
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=outside.started_at,
            ended_at=outside.ended_at,
            reason="Separate later evidence",
        )
    )
    validate_agent_result(result, manifest)
    assert result.episodes[0].evidence_ids == ["observation:one"]


def test_episode_without_overlapping_citation_is_rejected():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=26)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=27)
    with pytest.raises(
        TimelineIncompleteSegmentation, match="no temporally overlapping evidence"
    ):
        validate_agent_result(result, manifest)


def test_a_malformed_episode_is_dropped_without_losing_the_good_ones():
    """The whole day used to be discarded over one bad episode.

    Under --output-schema the agent's planning narration is schema-valid, so a stray
    entry can appear beside real episodes; rejecting the run threw all of them away.
    """

    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "kind": "task",
            "title": "Inspect Chronicle day inputs",
            # Cites real evidence but sits outside it, as the leaked planning entries did.
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    result.episodes = [narration, good]

    validate_agent_result(result, manifest)

    assert [episode.title for episode in result.episodes] == ["Watched Terminator"]


def test_dropping_a_malformed_episode_remaps_parent_indices():
    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "title": "planning noise",
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    child = good.model_copy(update={"title": "child", "parent_episode_index": 1})
    result.episodes = [narration, good, child]

    validate_agent_result(result, manifest)

    titles = [episode.title for episode in result.episodes]
    assert titles == ["Watched Terminator", "child"]
    # `good` moved from index 1 to 0, so the child's parent pointer must follow it.
    assert result.episodes[1].parent_episode_index == 0


def test_unsupported_episode_end_is_deterministically_bounded_to_cited_evidence():
    manifest = _manifest()
    manifest.evidence[0].ended_at = manifest.started_at + timedelta(minutes=5)
    result = _result()

    validate_agent_result(result, manifest)

    assert result.episodes[0].ended_at == manifest.evidence[0].ended_at


def test_unsupported_episode_start_is_deterministically_bounded_to_cited_evidence():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    validate_agent_result(result, manifest)

    assert result.episodes[0].started_at == manifest.evidence[0].started_at


def test_point_only_citation_cannot_be_repaired_into_a_positive_episode():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    manifest.evidence[0].ended_at = manifest.evidence[0].started_at
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    with pytest.raises(TimelineIncompleteSegmentation, match="positive cited interval"):
        validate_agent_result(result, manifest)


def test_naive_agent_timestamps_are_interpreted_as_utc():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = result.episodes[0].started_at.replace(tzinfo=None)
    result.episodes[0].ended_at = result.episodes[0].ended_at.replace(tzinfo=None)

    validate_agent_result(result, manifest)

    assert result.episodes[0].started_at.tzinfo == timezone.utc
    assert result.episodes[0].ended_at.tzinfo == timezone.utc


def test_unexplained_evidence_is_materialized_as_an_unassigned_interval():
    manifest = _manifest()
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=29),
        role="application_state",
    )
    manifest.evidence.append(later)

    result = _result()

    validate_agent_result(result, manifest)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].started_at == later.started_at
    assert result.unassigned_intervals[0].ended_at == later.ended_at
    assert result.unassigned_intervals[0].reason == (
        "Evidence was not assigned by semantic analysis"
    )


def test_unassigned_intervals_outside_manifest_are_discarded():
    manifest = _manifest()
    result = _result()
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=manifest.started_at - timedelta(minutes=1),
            ended_at=manifest.started_at,
            reason="Invalid outer interval",
        )
    )

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals == []


def test_pinned_intervals_account_for_their_evidence():
    """A day fully covered by a confirmed episode needs no drafted episode."""

    manifest = _manifest()
    result = TimelineAgentResult(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []
    assert result.unassigned_intervals == []


def test_evidence_outside_a_pinned_interval_is_still_unaccounted():
    manifest = _manifest()
    result = TimelineAgentResult(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=10),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_drafted_episode_repeating_a_pinned_interval_is_dropped():
    manifest = _manifest()
    result = _result()
    pinned = [(result.episodes[0].started_at, result.episodes[0].ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []


def test_a_draft_inside_a_pinned_interval_is_dropped_even_after_clamping():
    """Bound clamping can pull a draft into a confirmed interval; it is still a dupe."""

    manifest = _manifest()
    result = _result()
    # Cited evidence ends at minute 25, so this episode clamps back to 24-25 — entirely
    # inside the pinned interval, though it did not start that way.
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=24)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=29)
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []


def test_dropping_a_pinned_duplicate_remaps_parent_indices():
    manifest = _manifest()
    result = _result()
    duplicate = result.episodes[0].model_copy(
        update={
            "started_at": manifest.started_at + timedelta(minutes=2),
            "ended_at": manifest.started_at + timedelta(minutes=10),
        }
    )
    child = result.episodes[0].model_copy(
        update={
            "title": "Later event",
            "started_at": manifest.started_at + timedelta(minutes=12),
            "ended_at": manifest.started_at + timedelta(minutes=25),
            "parent_episode_index": 0,
        }
    )
    # Index 0 is the pinned duplicate; the survivor's parent index must follow it.
    result.episodes = [duplicate, child]
    pinned = [(duplicate.started_at, duplicate.ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert [episode.title for episode in result.episodes] == ["Later event"]
    assert result.episodes[0].parent_episode_index is None


def test_codex_usage_is_summed_across_turns():
    """An agentic run emits one turn.completed per turn; cost is their sum."""

    stdout = b"""{"type":"thread.started","thread_id":"t"}
{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,"reasoning_output_tokens":2}}
{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}
{"type":"turn.completed","usage":{"input_tokens":250,"cached_input_tokens":200,"output_tokens":30,"reasoning_output_tokens":5}}
"""

    assert _parse_usage(stdout) == {
        "input_tokens": 350,
        "cached_input_tokens": 240,
        "output_tokens": 40,
        "reasoning_output_tokens": 7,
        "turns": 2,
    }


def test_missing_or_malformed_usage_events_are_not_an_error():
    """Usage is accounting; losing it must never fail a completed analysis."""

    assert _parse_usage(b"") == {}
    assert _parse_usage(b"not json\n{broken\n") == {}
    assert _parse_usage(b'{"type":"turn.completed"}\n') == {"turns": 1}


def test_a_day_fully_covered_by_confirmed_episodes_may_add_nothing():
    """Pinned coverage is a real account of the day, so silence is legitimate."""

    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(TimelineAgentResult(episodes=[]), manifest, pinned)


def test_partially_pinned_coverage_still_rejects_unexplained_evidence():
    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=6),
        )
    ]
    result = TimelineAgentResult(episodes=[])

    validate_agent_result(result, manifest, pinned)

    # Not silently dropped: the unexplained tail is materialized as unknown time.
    assert result.unassigned_intervals


def test_unassigned_cause_distinguishes_blackout_from_unexplained_capture():
    """The agent's prose is not trusted for *why* an interval is unassigned.

    A run was observed labelling a multi-hour recording blackout "no evidence supports
    a coherent episode", which reads as a segmentation failure rather than a gap in
    capture. The cause is derived from the manifest instead.
    """

    manifest = _manifest()
    blackout = TimelineEvidenceItem(
        evidence_id="capture_gap:one",
        kind="capture_gap",
        started_at=manifest.started_at + timedelta(minutes=26),
        ended_at=manifest.ended_at,
        role="application_state",
        excerpt="no capture",
    )
    manifest.evidence.append(blackout)
    manifest.windows[0].evidence_ids.append(blackout.evidence_id)

    result = _result()
    # Leave the tail of the observation to the unassigned interval below.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=15)
    result.unassigned_intervals = [
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=15),
            ended_at=manifest.started_at + timedelta(minutes=25),
            reason="No evidence supports a coherent episode in this gap.",
        ),
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=26),
            ended_at=manifest.ended_at,
            reason="No evidence supports a coherent episode in this gap.",
        ),
    ]

    validate_agent_result(result, manifest)

    causes = {
        interval.started_at: interval.cause for interval in result.unassigned_intervals
    }
    # Overlaps the observation: captured, just not explained.
    assert causes[manifest.started_at + timedelta(minutes=15)] == "unexplained"
    # Overlaps only a capture_gap: nothing was recorded to explain.
    assert causes[manifest.started_at + timedelta(minutes=26)] == "no_capture"
