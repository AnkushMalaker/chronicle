from datetime import date, datetime, timedelta, timezone

import pytest

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
from advanced_omi_backend.services.timeline.executor import validate_agent_result


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


def test_valid_overlapping_episode_result_is_accepted():
    validate_agent_result(_result(), _manifest())


def test_invented_evidence_is_rejected():
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_agent_result(_result("observation:invented"), _manifest())


def test_empty_accounting_is_materialized_as_unassigned_evidence():
    result = TimelineAgentResult(episodes=[], unassigned_intervals=[])
    manifest = _manifest()

    validate_agent_result(result, manifest)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].started_at == manifest.evidence[0].started_at
    assert result.unassigned_intervals[0].ended_at == manifest.evidence[0].ended_at


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
    with pytest.raises(ValueError, match="no temporally overlapping evidence"):
        validate_agent_result(result, manifest)


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

    with pytest.raises(ValueError, match="positive cited interval"):
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
