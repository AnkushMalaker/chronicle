"""Keep recording coverage separate from human activities and review decisions."""

from datetime import datetime, timezone

from backend.models.timeline import TimelineDay

from .projection import affected_local_dates

MAX_QUIET_ACTIVE_FRACTION = 0.001  # At least 99.9% below the sound-activity threshold.
COVERAGE_KINDS = frozenset({"audio_span", "capture_gap"})


def recording_only_evidence(evidence) -> bool:
    """Only quiet, speech-free recorder bookkeeping is safe to remove automatically.

    Speech-bearing, unscored, and acoustically active spans remain reviewable even
    when their transcript has not yet been attached to the episode.
    """
    if not evidence or any(item.kind not in COVERAGE_KINDS for item in evidence):
        return False
    for item in evidence:
        if item.kind == "capture_gap":
            continue
        if item.metadata.get("state") != "no_speech":
            return False
        fractions = item.metadata.get("acoustic_active_fraction") or []
        if fractions and sum(fractions) / len(fractions) > MAX_QUIET_ACTIVE_FRACTION:
            return False
        active = item.metadata.get("acoustic_active_seconds")
        if active is not None:
            duration = (
                (_utc(item.ended_at) - _utc(item.started_at)).total_seconds()
                if item.ended_at
                else 0
            )
            if duration <= 0 or active / duration > MAX_QUIET_ACTIVE_FRACTION:
                return False
    return True


def episode_is_recording_only(episode) -> bool:
    # Explicit human activity labels supply meaning beyond recorder metadata.
    if {"title", "kind"} & set(episode.confirmed_fields):
        return False
    return recording_only_evidence(episode.evidence_refs)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def rejection_basis(episode) -> dict:
    return {
        "started_at": _utc(episode.started_at).isoformat(),
        "ended_at": _utc(episode.ended_at).isoformat(),
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "content_hash": item.content_hash,
                "kind": item.kind,
            }
            for item in episode.evidence_refs
        ],
    }


def rejected_activity(started_at, ended_at, evidence, decisions) -> bool:
    """Reject the same evidence within the rejected span, not all overlapping life.

    New evidence or changed content is allowed to propose a new interpretation.
    Titles, generated episode IDs and model confidence cannot bypass the decision.
    """
    meaningful = [item for item in evidence if item.kind not in COVERAGE_KINDS]
    candidate = {
        (item.evidence_id, item.content_hash) for item in (meaningful or evidence)
    }
    if not candidate:
        return False
    for decision in decisions:
        if decision.action != "episode_not_activity":
            continue
        basis = decision.after["rejected_activity"]
        if not (
            _utc(basis["started_at"]) <= _utc(started_at)
            and _utc(ended_at) <= _utc(basis["ended_at"])
        ):
            continue
        rejected_items = [
            item for item in basis["evidence"] if item["kind"] not in COVERAGE_KINDS
        ] or basis["evidence"]
        rejected = {
            (item["evidence_id"], item["content_hash"]) for item in rejected_items
        }
        if candidate <= rejected:
            return True
    return False


async def activity_policy_days(user_id, started_at, ended_at, timezone_name):
    dates = affected_local_dates(started_at, ended_at, timezone_name)
    return await TimelineDay.find(
        TimelineDay.user_id == user_id,
        TimelineDay.timezone == timezone_name,
        {"local_date": {"$in": dates}},
    ).to_list()


async def retire_recording_only_episodes(day):
    """Apply the coverage rule to an existing snapshot using normal audited publication."""
    # These publication modules import activity policy for their filtering rules.
    from .consolidation import snapshot_episodes
    from .manual_publication import publish_manual_episode_change

    episodes = await snapshot_episodes(day)
    coverage = [episode for episode in episodes if episode_is_recording_only(episode)]
    if coverage:
        await publish_manual_episode_change(
            day=day,
            predecessors=coverage,
            successors=[],
            action="episode_coverage_only",
            before={
                "episodes": [
                    dict(episode_id=e.episode_id, title=e.title, **rejection_basis(e))
                    for e in coverage
                ]
            },
            after={
                "reason": "Recording coverage alone does not establish an activity.",
                "raw_recordings_preserved": True,
            },
        )
    return [episode.episode_id for episode in coverage]
