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


def test_empty_accounting_is_rejected_when_evidence_exists():
    result = TimelineAgentResult(episodes=[], unassigned_intervals=[])
    with pytest.raises(ValueError, match="accounts for no evidence intervals"):
        validate_agent_result(result, _manifest())


def test_unavailable_representative_image_is_dropped_without_losing_episode():
    result = _result()
    result.episodes[0].representative_evidence_id = "observation:one"
    validate_agent_result(result, _manifest())
    assert result.episodes[0].representative_evidence_id is None
