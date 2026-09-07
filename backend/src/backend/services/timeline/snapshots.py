"""Canonical content-addressed Timeline day snapshots.

Snapshot identity is deliberately independent from an analysis run and from operational
timestamps.  It names the exact episode/group revisions and evidence state rendered by
one user-local day, including the day identity so the two projections of a
cross-midnight episode cannot collide.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from backend.models.timeline import (
    EpisodeRevisionRef,
    GroupRevisionRef,
    TimelineDaySnapshot,
    TimelineEpisode,
    TimelineSemanticGroupRevision,
    utcnow,
)
from backend.services.inference_artifacts import canonical_hash

from .timezone import canonical_timezone

SNAPSHOT_SCHEMA_VERSION = "timeline-day-snapshot-v1"
EVIDENCE_STATE_SCHEMA_VERSION = "timeline-evidence-state-v1"


def _bson_utc_timestamp(value: datetime) -> str:
    """Canonicalize a datetime to MongoDB's UTC millisecond precision."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    utc_value = aware.astimezone(timezone.utc).replace(
        microsecond=(aware.microsecond // 1000) * 1000
    )
    return utc_value.isoformat(timespec="milliseconds")


def _stable_evidence_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _bson_utc_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _stable_evidence_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable_evidence_value(item) for item in value]
    return value


def _episode_sort_key(item: EpisodeRevisionRef) -> tuple[str, int]:
    return item.episode_key, item.revision


def _group_sort_key(item: GroupRevisionRef) -> tuple[str, str, int]:
    return item.owner_local_date.isoformat(), item.group_key, item.revision


def canonical_snapshot_payload(
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
    evidence_state_hash: str,
    episode_revisions: Iterable[EpisodeRevisionRef],
    semantic_group_revisions: Iterable[GroupRevisionRef] = (),
) -> dict[str, Any]:
    """Return the schema-versioned payload whose hash is ``snapshot_id``."""

    timezone_name = canonical_timezone(timezone_name)
    episodes = sorted(list(episode_revisions), key=_episode_sort_key)
    groups = sorted(list(semantic_group_revisions), key=_group_sort_key)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "user_id": user_id,
        "local_date": local_date.isoformat(),
        "timezone": timezone_name,
        "evidence_state_hash": evidence_state_hash,
        "episode_revisions": [item.model_dump(mode="json") for item in episodes],
        "semantic_group_revisions": [item.model_dump(mode="json") for item in groups],
    }


def build_day_snapshot(
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
    evidence_state_hash: str,
    episode_revisions: Iterable[EpisodeRevisionRef],
    semantic_group_revisions: Iterable[GroupRevisionRef] = (),
    created_at: datetime | None = None,
) -> TimelineDaySnapshot:
    """Build and hash one canonical snapshot, rejecting ambiguous active identities."""

    episodes = sorted(list(episode_revisions), key=_episode_sort_key)
    groups = sorted(list(semantic_group_revisions), key=_group_sort_key)
    episode_keys = [item.episode_key for item in episodes]
    if len(episode_keys) != len(set(episode_keys)):
        raise ValueError("cannot snapshot multiple active revisions of one episode key")
    group_keys = [(item.owner_local_date, item.group_key) for item in groups]
    if len(group_keys) != len(set(group_keys)):
        raise ValueError("cannot snapshot multiple active revisions of one group key")
    payload = canonical_snapshot_payload(
        user_id=user_id,
        local_date=local_date,
        timezone_name=timezone_name,
        evidence_state_hash=evidence_state_hash,
        episode_revisions=episodes,
        semantic_group_revisions=groups,
    )
    return TimelineDaySnapshot(
        snapshot_id=canonical_hash(payload),
        episode_revisions=episodes,
        semantic_group_revisions=groups,
        evidence_state_hash=evidence_state_hash,
        created_at=created_at or utcnow(),
    )


def verify_day_snapshot(
    snapshot: TimelineDaySnapshot,
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
) -> None:
    """Fail closed when an embedded snapshot does not match its canonical payload."""

    payload = canonical_snapshot_payload(
        user_id=user_id,
        local_date=local_date,
        timezone_name=timezone_name,
        evidence_state_hash=snapshot.evidence_state_hash,
        episode_revisions=snapshot.episode_revisions,
        semantic_group_revisions=snapshot.semantic_group_revisions,
    )
    expected = canonical_hash(payload)
    if snapshot.snapshot_id != expected:
        raise ValueError(
            f"snapshot hash mismatch: expected {expected}, got {snapshot.snapshot_id}"
        )


def evidence_state_hash_for_episodes(
    episodes: Sequence[TimelineEpisode],
    *,
    authorized_range_revisions: Mapping[str, int | str] | None = None,
) -> str:
    """Hash the evidence fences carried by exact projected episode revisions.

    Callers with a richer manifest can add its authorized range revisions.  The
    episode references retain locator and resolved-boundary provenance, so a source
    content or anchor-resolution change necessarily produces another evidence hash.
    """

    referenced: dict[str, dict[str, Any]] = {}
    evidence_revisions: set[int] = set()
    for episode in sorted(episodes, key=lambda item: (item.episode_key, item.revision)):
        if episode.evidence_revision is not None:
            evidence_revisions.add(int(episode.evidence_revision))
        for ref in sorted(episode.evidence_refs, key=lambda item: item.evidence_id):
            payload = {
                "evidence_id": ref.evidence_id,
                "kind": ref.kind,
                "source_id": ref.source_id,
                "source_item_id": ref.source_item_id,
                "started_at": _bson_utc_timestamp(ref.started_at),
                "ended_at": (
                    _bson_utc_timestamp(ref.ended_at) if ref.ended_at else None
                ),
                "role": ref.role,
                "excerpt": ref.excerpt,
                "content_hash": ref.content_hash,
                "ephemeral": ref.ephemeral,
                "locator": (
                    ref.locator.model_dump(mode="json") if ref.locator else None
                ),
                "start_boundary_support": sorted(
                    (
                        _stable_evidence_value(item.model_dump(mode="python"))
                        for item in ref.start_boundary_support
                    ),
                    key=lambda item: item["anchor_id"],
                ),
                "end_boundary_support": sorted(
                    (
                        _stable_evidence_value(item.model_dump(mode="python"))
                        for item in ref.end_boundary_support
                    ),
                    key=lambda item: item["anchor_id"],
                ),
                "metadata": _stable_evidence_value(ref.metadata),
            }
            # The same source may support concurrent episode claims. Hash the unique
            # source facts once; member identity belongs to the outer snapshot payload.
            referenced[canonical_hash(payload)] = payload
    return canonical_hash(
        {
            "schema_version": EVIDENCE_STATE_SCHEMA_VERSION,
            "evidence_revisions": sorted(evidence_revisions),
            "references": [referenced[key] for key in sorted(referenced)],
            "authorized_range_revisions": dict(
                sorted((authorized_range_revisions or {}).items())
            ),
        }
    )


def snapshot_from_projection(
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
    episodes: Sequence[TimelineEpisode],
    semantic_group_revisions: Sequence[TimelineSemanticGroupRevision] = (),
    authorized_range_revisions: Mapping[str, int | str] | None = None,
    created_at: datetime | None = None,
) -> TimelineDaySnapshot:
    """Build a snapshot from the exact active rows rendered by the day projection."""

    episode_refs = [
        EpisodeRevisionRef(episode_key=item.episode_key, revision=item.revision)
        for item in episodes
    ]
    group_refs = [
        GroupRevisionRef(
            owner_local_date=local_date,
            group_key=item.group_key,
            revision=item.revision,
        )
        for item in semantic_group_revisions
    ]
    return build_day_snapshot(
        user_id=user_id,
        local_date=local_date,
        timezone_name=timezone_name,
        evidence_state_hash=evidence_state_hash_for_episodes(
            episodes,
            authorized_range_revisions=authorized_range_revisions,
        ),
        episode_revisions=episode_refs,
        semantic_group_revisions=group_refs,
        created_at=created_at,
    )
