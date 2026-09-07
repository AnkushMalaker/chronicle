"""Snapshot-CAS publication for human Timeline episode revisions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from backend.models.timeline import (
    EpisodeRevisionRef,
    GroupRevisionRef,
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationDayPlan,
    TimelinePublicationOperation,
    TimelineReviewDecision,
    utcnow,
)

from .projection import affected_local_dates
from .publication import (
    apply_group_revision,
    build_publication_operation,
    publish_timeline_revision,
)
from .snapshots import build_day_snapshot, evidence_state_hash_for_episodes


class ManualPublicationConflict(RuntimeError):
    """A human edit was based on a stale canonical day snapshot."""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _intersects_day(episode: TimelineEpisode, day: TimelineDay) -> bool:
    zone = ZoneInfo(day.timezone)
    start = datetime.combine(day.local_date, time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        day.local_date.fromordinal(day.local_date.toordinal() + 1),
        time.min,
        tzinfo=zone,
    ).astimezone(timezone.utc)
    return _utc(episode.started_at) < end and _utc(episode.ended_at) > start


async def _exact_snapshot_episodes(day: TimelineDay) -> list[TimelineEpisode]:
    if day.current_snapshot is None:
        raise ManualPublicationConflict("Timeline day has no canonical snapshot")
    keys = [item.episode_key for item in day.current_snapshot.episode_revisions]
    rows = await TimelineEpisode.find(
        TimelineEpisode.user_id == day.user_id,
        {"episode_key": {"$in": keys}},
    ).to_list()
    by_ref = {(item.episode_key, item.revision): item for item in rows}
    try:
        return [
            by_ref[(item.episode_key, item.revision)]
            for item in day.current_snapshot.episode_revisions
        ]
    except KeyError as error:
        raise ManualPublicationConflict(
            "Timeline snapshot references an unavailable episode revision"
        ) from error


async def day_for_exact_episode(episode: TimelineEpisode) -> TimelineDay:
    """Resolve the current snapshot that owns exactly this episode revision."""

    days = await TimelineDay.find(
        TimelineDay.user_id == episode.user_id,
        TimelineDay.timezone == episode.timezone,
    ).to_list()
    exact = (episode.episode_key, episode.revision)
    matches = [
        day
        for day in days
        if day.current_snapshot is not None
        and exact
        in {
            (item.episode_key, item.revision)
            for item in day.current_snapshot.episode_revisions
        }
    ]
    if not matches:
        raise ManualPublicationConflict(
            "Episode revision is no longer in a current Timeline snapshot"
        )
    home = next((day for day in matches if day.local_date == episode.local_date), None)
    return home or matches[0]


async def publish_manual_episode_change(
    *,
    day: TimelineDay,
    predecessors: Sequence[TimelineEpisode],
    successors: Sequence[TimelineEpisode],
    action: Literal[
        "episode_update",
        "episode_structure_confirm",
        "episode_split",
        "episode_merge",
        "episode_delete",
        "episode_not_activity",
        "episode_coverage_only",
    ],
    before: dict,
    after: dict,
) -> list[TimelineEpisode]:
    """Install immutable successors and every affected day projection atomically."""

    if day.current_snapshot is None:
        raise ManualPublicationConflict("Timeline day has no canonical snapshot")
    if day.review_state in {"memory_generating", "memory_applying"}:
        raise ManualPublicationConflict("Timeline review is currently being applied")
    current = await _exact_snapshot_episodes(day)
    predecessor_refs = {(item.episode_key, item.revision) for item in predecessors}
    active_refs = {(item.episode_key, item.revision) for item in current}
    if not predecessor_refs <= active_refs:
        raise ManualPublicationConflict("Episode changed before the edit was published")

    candidate_days = await TimelineDay.find(
        TimelineDay.user_id == day.user_id,
        TimelineDay.timezone == day.timezone,
        {"current_snapshot_id": {"$nin": [None, ""]}},
    ).to_list()
    candidate_by_date = {item.local_date: item for item in candidate_days}
    affected_dates = sorted(
        {
            local_date
            for episode in [*predecessors, *successors]
            for local_date in affected_local_dates(
                episode.started_at, episode.ended_at, day.timezone
            )
        }
    )
    plans: list[TimelinePublicationDayPlan] = []
    rebased_groups = {}
    successor_by_key = {item.episode_key: item for item in successors}
    decision_created_at = utcnow()
    for local_date in affected_dates:
        affected_day = candidate_by_date.get(local_date)
        if affected_day is None:
            result_episodes = sorted(
                [
                    successor
                    for successor in successors
                    if local_date
                    in affected_local_dates(
                        successor.started_at, successor.ended_at, day.timezone
                    )
                ],
                key=lambda item: (_utc(item.started_at), item.episode_key),
            )
            resulting_snapshot = build_day_snapshot(
                user_id=day.user_id,
                local_date=local_date,
                timezone_name=day.timezone,
                evidence_state_hash=evidence_state_hash_for_episodes(result_episodes),
                episode_revisions=[
                    EpisodeRevisionRef(
                        episode_key=item.episode_key, revision=item.revision
                    )
                    for item in result_episodes
                ],
            )
            plans.append(
                TimelinePublicationDayPlan(
                    local_date=local_date,
                    timezone=day.timezone,
                    base_snapshot_id=None,
                    resulting_snapshot=resulting_snapshot,
                    review_decision=TimelineReviewDecision(
                        run_id=resulting_snapshot.snapshot_id,
                        action=action,
                        episode_ids=[item.episode_id for item in predecessors],
                        before=before,
                        after=after,
                        created_at=decision_created_at,
                    ),
                )
            )
            continue
        snapshot = affected_day.current_snapshot
        if snapshot is None:
            continue
        snapshot_refs = {
            (item.episode_key, item.revision) for item in snapshot.episode_revisions
        }
        if not (snapshot_refs & predecessor_refs) and not any(
            _intersects_day(successor, affected_day) for successor in successors
        ):
            continue
        if affected_day.review_state in {"memory_generating", "memory_applying"}:
            raise ManualPublicationConflict(
                "Timeline review is currently being applied on an affected day"
            )
        affected_episodes = await _exact_snapshot_episodes(affected_day)
        result_by_ref = {
            (item.episode_key, item.revision): item
            for item in affected_episodes
            if (item.episode_key, item.revision) not in predecessor_refs
        }
        for successor in successors:
            if _intersects_day(successor, affected_day):
                result_by_ref[(successor.episode_key, successor.revision)] = successor
        result_episodes = sorted(
            result_by_ref.values(),
            key=lambda item: (_utc(item.started_at), item.episode_key),
        )

        group_refs = []
        for group_ref in snapshot.semantic_group_revisions:
            key = (group_ref.owner_local_date, group_ref.group_key, group_ref.revision)
            owner = candidate_by_date.get(group_ref.owner_local_date)
            original = (
                next(
                    (
                        item
                        for item in owner.semantic_group_history
                        if item.group_key == group_ref.group_key
                        and item.revision == group_ref.revision
                    ),
                    None,
                )
                if owner
                else None
            )
            if (
                action
                in {
                    "episode_structure_confirm",
                    "episode_not_activity",
                    "episode_coverage_only",
                }
                and original
                and any(
                    (ref.episode_key, ref.revision) in predecessor_refs
                    for ref in original.member_revisions
                )
            ):
                if key not in rebased_groups:
                    revised = original.model_copy(deep=True)
                    revised.revision = (
                        max(
                            item.revision
                            for item in owner.semantic_group_history
                            if item.group_key == original.group_key
                        )
                        + 1
                    )
                    revised.predecessor_revisions = [group_ref]
                    revised.source_snapshot_id = owner.current_snapshot_id
                    revised.created_at = decision_created_at
                    if action != "episode_structure_confirm":
                        remaining = [
                            (ref, eid)
                            for ref, eid in zip(
                                original.member_revisions, original.episode_ids
                            )
                            if (ref.episode_key, ref.revision) not in predecessor_refs
                        ]
                        if len(remaining) < 2:
                            continue
                        revised.member_revisions = [ref for ref, _ in remaining]
                        revised.episode_ids = [eid for _, eid in remaining]
                        remaining_episodes = await TimelineEpisode.find(
                            TimelineEpisode.user_id == day.user_id,
                            {"episode_id": {"$in": revised.episode_ids}},
                        ).to_list()
                        revised.started_at = min(
                            _utc(item.started_at) for item in remaining_episodes
                        )
                        revised.ended_at = max(
                            _utc(item.ended_at) for item in remaining_episodes
                        )
                        revised.summary = f"These {len(remaining)} episodes remain in the accepted grouping after recording coverage or a rejected activity was removed."
                    else:
                        revised.member_revisions = [
                            (
                                EpisodeRevisionRef(
                                    episode_key=ref.episode_key,
                                    revision=successor_by_key[ref.episode_key].revision,
                                )
                                if (ref.episode_key, ref.revision) in predecessor_refs
                                else ref
                            )
                            for ref in original.member_revisions
                        ]
                        revised.episode_ids = [
                            (
                                successor_by_key[ref.episode_key].episode_id
                                if (ref.episode_key, ref.revision) in predecessor_refs
                                else episode_id
                            )
                            for ref, episode_id in zip(
                                original.member_revisions, original.episode_ids
                            )
                        ]
                    rebased_groups[key] = (owner, revised)
                group_refs.append(
                    GroupRevisionRef(
                        owner_local_date=group_ref.owner_local_date,
                        group_key=group_ref.group_key,
                        revision=rebased_groups[key][1].revision,
                    )
                )
            else:
                group_refs.append(group_ref)

        resulting_snapshot = build_day_snapshot(
            user_id=affected_day.user_id,
            local_date=affected_day.local_date,
            timezone_name=affected_day.timezone,
            evidence_state_hash=evidence_state_hash_for_episodes(result_episodes),
            episode_revisions=[
                EpisodeRevisionRef(episode_key=item.episode_key, revision=item.revision)
                for item in result_episodes
            ],
            semantic_group_revisions=group_refs,
        )
        plans.append(
            TimelinePublicationDayPlan(
                local_date=affected_day.local_date,
                timezone=affected_day.timezone,
                base_snapshot_id=snapshot.snapshot_id,
                resulting_snapshot=resulting_snapshot,
                review_decision=TimelineReviewDecision(
                    run_id=resulting_snapshot.snapshot_id,
                    action=action,
                    episode_ids=[item.episode_id for item in predecessors],
                    before=before,
                    after=after,
                    created_at=decision_created_at,
                ),
            )
        )
    if not plans:
        raise ManualPublicationConflict(
            "No current Timeline projection contains the edit"
        )
    operations: list[TimelinePublicationOperation] = []
    for successor in successors:
        successor.id = None
        # Mongo returns naive UTC datetimes while newly edited bounds are aware. The
        # immutable journal must carry one unambiguous clock representation so replay
        # validates the same document after either source shape.
        successor.started_at = _utc(successor.started_at)
        successor.ended_at = _utc(successor.ended_at)
        successor.created_at = _utc(successor.created_at)
        successor.revised_at = _utc(successor.revised_at)
        operations.append(
            build_publication_operation(
                sequence=len(operations),
                kind="insert_episode_revision",
                expected_revision=max(0, successor.revision - 1),
                payload={"episode": successor.model_dump(mode="json", exclude={"id"})},
            )
        )
    for owner, revision in rebased_groups.values():
        operations.append(
            build_publication_operation(
                sequence=len(operations),
                kind="insert_group_revision",
                expected_revision=revision.revision - 1,
                payload={
                    "local_date": owner.local_date.isoformat(),
                    "timezone": owner.timezone,
                    "owner_id": str(owner.id),
                    "revision": revision.model_dump(mode="json"),
                },
            )
        )

    successor_keys = sorted({item.episode_key for item in successors})
    for predecessor in predecessors:
        operations.append(
            build_publication_operation(
                sequence=len(operations),
                kind="supersede_episode_revision",
                expected_revision=predecessor.revision,
                payload={
                    "episode_key": predecessor.episode_key,
                    "revision": predecessor.revision,
                    "successor_keys": (
                        [predecessor.episode_key]
                        if action == "episode_structure_confirm"
                        else successor_keys
                    ),
                },
            )
        )

    async def apply(operation: TimelinePublicationOperation):
        if operation.kind == "insert_group_revision":
            collection = TimelineDay.get_pymongo_collection()
            owner_id = PydanticObjectId(operation.payload["owner_id"])
            current = await collection.find_one(
                {
                    "_id": owner_id,
                    "user_id": day.user_id,
                    "pending_publication_id": {"$nin": [None, ""]},
                }
            )
            if current is None:
                return "conflict"
            return await apply_group_revision(
                day.user_id, current["pending_publication_id"], operation
            )
        if operation.kind == "insert_episode_revision":
            successor = TimelineEpisode.model_validate(operation.payload["episode"])
            existing = await TimelineEpisode.find_one(
                TimelineEpisode.user_id == day.user_id,
                TimelineEpisode.episode_key == successor.episode_key,
                TimelineEpisode.revision == successor.revision,
            )
            if existing is not None:
                return (
                    "already_applied"
                    if existing.episode_id == successor.episode_id
                    else "conflict"
                )
            await successor.insert()
            return "applied"
        if operation.kind != "supersede_episode_revision":
            return "conflict"
        key = operation.payload["episode_key"]
        revision = operation.payload["revision"]
        existing = await TimelineEpisode.find_one(
            TimelineEpisode.user_id == day.user_id,
            TimelineEpisode.episode_key == key,
            TimelineEpisode.revision == revision,
        )
        if existing is None:
            return "conflict"
        desired = set(operation.payload["successor_keys"])
        if existing.status == "superseded":
            return (
                "already_applied"
                if desired <= set(existing.successor_keys)
                else "conflict"
            )
        collection = TimelineEpisode.get_pymongo_collection()
        result = await collection.update_one(
            {
                "user_id": day.user_id,
                "episode_key": key,
                "revision": revision,
                "status": {"$ne": "superseded"},
            },
            {
                "$set": {"status": "superseded", "revised_at": utcnow()},
                "$addToSet": {"successor_keys": {"$each": sorted(desired)}},
            },
        )
        return "applied" if result.modified_count == 1 else "conflict"

    await publish_timeline_revision(
        user_id=day.user_id,
        operation_source="manual",
        affected_days=plans,
        operations=operations,
        apply_operation=apply,
    )
    return list(successors)
