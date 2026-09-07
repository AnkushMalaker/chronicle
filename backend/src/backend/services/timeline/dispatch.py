"""Stable-Episode dispatch of plugins and bounded detailed summaries.

A recording close is evidence, not a semantic claim. Rolling source Conversations get
neither semantic summaries nor memory. A settled or structurally human-confirmed
conversational Episode may enqueue one scope-fenced detailed summary when its exact
revision is in a ready/reviewed day snapshot. Only settlement dispatches the user-facing
completion event. Explicitly reviewed selections independently own vault proposals. Latches are claimed before side effects and released on failure so recovery
remains at-least-once.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from pymongo.errors import DuplicateKeyError

from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.models.timeline import (
    EpisodeDispatchLatch,
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationJournal,
    utcnow,
)
from backend.plugins.events import PluginEvent
from backend.services.memory.visibility import conversation_scope_filter
from backend.services.plugin_service import dispatch_plugin_event
from backend.services.sse_publisher import publish_sse_event

from .episode_summary import (
    SUMMARY_DAY_STATES,
    episode_revision_is_published,
    episode_structure_is_stable,
    episode_summary_eligibility,
)
from .recording_refs import episode_conversation_ids, resolve_live_recordings

logger = logging.getLogger(__name__)

CONVERSATION_COMPLETE = PluginEvent.CONVERSATION_COMPLETE.value
EPISODE_DETAILED_SUMMARY = "episode.detailed_summary"
SUMMARY_DISPATCH_LEASE = timedelta(minutes=10)


async def _related_conversations(episode: TimelineEpisode) -> list[Conversation]:
    source_ids = episode_conversation_ids(episode)
    if not source_ids:
        return []
    live_ids = await resolve_live_recordings(source_ids)
    if not live_ids:
        return []
    conversations = await Conversation.find(
        {
            "$and": [conversation_scope_filter()],
            "conversation_id": {"$in": sorted(live_ids)},
            "deleted": {"$ne": True},
            "memory_excluded": {"$ne": True},
        }
    ).to_list()
    by_id = {item.conversation_id: item for item in conversations}
    ordered_ids = [item for item in source_ids if item in live_ids]
    ordered_ids.extend(sorted(live_ids.difference(ordered_ids)))
    return [by_id[item] for item in ordered_ids if item in by_id]


async def _fire_plugins(
    episode: TimelineEpisode, conversations: list[Conversation]
) -> None:
    """Publish the user-facing completion event only after Episode settlement."""

    transcript = "\n".join(
        conversation.transcript
        for conversation in conversations
        if conversation.transcript
    )
    conversation_ids = [item.conversation_id for item in conversations]
    await dispatch_plugin_event(
        event=PluginEvent.CONVERSATION_COMPLETE,
        user_id=episode.user_id,
        data={
            "conversation": {
                "client_id": "",
                "user_id": episode.user_id,
            },
            "transcript": transcript,
            "duration": (episode.ended_at - episode.started_at).total_seconds(),
            "conversation_id": (
                conversation_ids[0] if conversation_ids else episode.episode_id
            ),
            "episode_key": episode.episode_key,
            "episode_id": episode.episode_id,
            "started_at": episode.started_at,
            "ended_at": episode.ended_at,
            "title": episode.title,
            "summary": episode.summary,
            "related_conversation_ids": conversation_ids,
        },
        metadata={
            "source": "timeline_episode",
            "episode_key": episode.episode_key,
            "revision": int(episode.revision),
        },
        description=(
            f"episode={episode.episode_key[:12]}, revision={episode.revision}, "
            f"conversations={len(conversations)}"
        ),
        require_router=True,
    )


async def _enqueue_episode_summary_job(
    episode: TimelineEpisode,
    *,
    scope_hash: str,
    event_type: str,
    claim_token: str,
) -> None:
    """Queue a transcript-grounded account for this exact semantic revision."""

    # Queue startup reaches workers.timeline_jobs and reconciliation, which imports dispatch.
    from backend.controllers.queue_controller import (
        DETAILED_SUMMARY_JOB_TIMEOUT_SECONDS,
        JOB_RESULT_TTL,
        summary_queue,
    )
    from backend.workers.conversation_jobs import generate_episode_detailed_summary_job

    eligibility = await episode_summary_eligibility(episode)
    if not eligibility.eligible or eligibility.scope_hash != scope_hash:
        raise RuntimeError(
            f"episode summary eligibility changed before enqueue: {eligibility.reason}"
        )

    summary_queue.enqueue(
        generate_episode_detailed_summary_job,
        episode.episode_id,
        int(episode.revision),
        scope_hash,
        event_type,
        claim_token,
        job_timeout=DETAILED_SUMMARY_JOB_TIMEOUT_SECONDS,
        result_ttl=JOB_RESULT_TTL,
        job_id=(
            f"episode_detailed_summary_{episode.episode_id[:12]}_"
            f"{episode.revision}_{claim_token[:12]}"
        ),
        description=f"Generate detailed summary for episode {episode.episode_id[:8]}",
    )


async def _claim_and_fire(
    episode: TimelineEpisode,
    event_type: str,
    fire: Callable[[TimelineEpisode, list[Conversation]], Awaitable[None]],
    conversations: list[Conversation],
    *,
    latch_key: str | None = None,
) -> bool:
    subject_key = latch_key or episode.episode_key
    latch = EpisodeDispatchLatch(
        user_id=episode.user_id,
        episode_key=subject_key,
        event_type=event_type,
        episode_id=episode.episode_id,
        revision=int(episode.revision),
        claim_token=str(uuid.uuid4()),
    )
    try:
        await latch.insert()
    except DuplicateKeyError:
        logger.debug(
            "🩹 %s already dispatched for episode %s",
            event_type,
            subject_key,
        )
        return False

    try:
        await fire(episode, conversations)
    except Exception:
        await EpisodeDispatchLatch.find(
            EpisodeDispatchLatch.episode_key == subject_key,
            EpisodeDispatchLatch.event_type == event_type,
        ).delete()
        logger.error(
            "❌ Dispatch of %s failed for episode %s; latch released for retry",
            event_type,
            subject_key,
            exc_info=True,
        )
        return False
    return True


async def _retype_media_source_record(episode: TimelineEpisode) -> None:
    """A media/noise episode must not leave a false conversation note behind.

    TODO(WP-B): the vault mutation belongs to the typed write orchestration that owns
    ``Conversations/<id>.md``. ``services/memory/conversation_note.py`` only renders a
    note into a path a writer already chose; retyping means locating the existing note
    in the active vault policy, rewriting its category, and auditing the change. Doing
    that from here would invent a second vault writer, so this records the decision and
    leaves the mutation to the vault-projection package.
    """

    logger.info(
        "🩹 Settled non-conversational episode %s (kind=%s) dispatches nothing; "
        "source record retype deferred to the vault projection (WP-B)",
        episode.episode_key,
        episode.kind,
    )


async def enqueue_episode_detailed_summary(episode: TimelineEpisode) -> bool:
    """Lease one summary attempt; only a materialized matching scope is complete."""

    eligibility = await episode_summary_eligibility(episode)
    if not eligibility.eligible or not eligibility.scope_hash:
        return False
    if (
        episode.detailed_summary
        and episode.detailed_summary_scope_hash == eligibility.scope_hash
        and episode.detailed_summary_revision == int(episode.revision)
    ):
        return False

    event_type = (
        f"{EPISODE_DETAILED_SUMMARY}:{episode.revision}:{eligibility.scope_hash}"
    )
    claim_token = str(uuid.uuid4())
    now = utcnow()
    latch = EpisodeDispatchLatch(
        user_id=episode.user_id,
        episode_key=episode.episode_key,
        event_type=event_type,
        episode_id=episode.episode_id,
        revision=int(episode.revision),
        claim_token=claim_token,
        dispatched_at=now,
    )
    try:
        await latch.insert()
    except DuplicateKeyError:
        cutoff = now - SUMMARY_DISPATCH_LEASE
        reclaimed = (
            await EpisodeDispatchLatch.get_pymongo_collection().find_one_and_update(
                {
                    "episode_key": episode.episode_key,
                    "event_type": event_type,
                    "dispatched_at": {"$lte": cutoff},
                },
                {
                    "$set": {
                        "user_id": episode.user_id,
                        "episode_id": episode.episode_id,
                        "revision": int(episode.revision),
                        "claim_token": claim_token,
                        "dispatched_at": now,
                    }
                },
            )
        )
        if reclaimed is None:
            return False
    try:
        await _enqueue_episode_summary_job(
            episode,
            scope_hash=eligibility.scope_hash,
            event_type=event_type,
            claim_token=claim_token,
        )
    except Exception:
        await release_episode_summary_claim(event_type, claim_token)
        logger.error(
            "❌ Summary enqueue failed for episode %s; lease released for retry",
            episode.episode_key,
            exc_info=True,
        )
        return False
    return True


async def release_episode_summary_claim(event_type: str, claim_token: str) -> None:
    """Release only the exact attempt, never a newer recovery lease."""

    await EpisodeDispatchLatch.get_pymongo_collection().delete_one(
        {
            "event_type": event_type,
            "claim_token": claim_token,
        }
    )


async def _current_journal_episodes(
    journal: TimelinePublicationJournal,
) -> list[TimelineEpisode]:
    """Resolve only refs still installed by this committed publication."""

    if not journal.affected_days:
        return []
    day_clauses = [
        {
            "user_id": journal.user_id,
            "local_date": plan.local_date,
            "timezone": plan.timezone,
            "current_snapshot_id": plan.resulting_snapshot.snapshot_id,
            "snapshot_state": {"$in": sorted(SUMMARY_DAY_STATES)},
            "pending_publication_id": {"$in": [None, ""]},
        }
        for plan in journal.affected_days
    ]
    current_days = await TimelineDay.find({"$or": day_clauses}).to_list()
    current_snapshot_ids = {
        day.current_snapshot_id for day in current_days if day.current_snapshot_id
    }
    refs: dict[tuple[str, int], None] = {}
    for plan in journal.affected_days:
        if plan.resulting_snapshot.snapshot_id not in current_snapshot_ids:
            continue
        for ref in plan.resulting_snapshot.episode_revisions:
            refs[(ref.episode_key, int(ref.revision))] = None
    if not refs:
        return []
    episodes = await TimelineEpisode.find(
        {
            "user_id": journal.user_id,
            "$or": [
                {"episode_key": episode_key, "revision": revision}
                for episode_key, revision in refs
            ],
        }
    ).to_list()
    by_ref = {
        (episode.episode_key, int(episode.revision)): episode for episode in episodes
    }
    return [by_ref[ref] for ref in refs if ref in by_ref]


async def _summary_lease_is_active(
    episode: TimelineEpisode, scope_hash: str, *, now: datetime
) -> bool:
    event_type = f"{EPISODE_DETAILED_SUMMARY}:{episode.revision}:{scope_hash}"
    latch = await EpisodeDispatchLatch.get_pymongo_collection().find_one(
        {
            "user_id": episode.user_id,
            "episode_key": episode.episode_key,
            "episode_id": episode.episode_id,
            "revision": int(episode.revision),
            "event_type": event_type,
            "dispatched_at": {"$gt": now - SUMMARY_DISPATCH_LEASE},
        },
        {"_id": 1},
    )
    return latch is not None


async def _episode_dispatch_state(
    episode: TimelineEpisode, *, now: datetime
) -> tuple[bool, bool]:
    """Return ``(due_now, terminal)`` for one exact current revision."""

    if not episode.conversational or not episode_structure_is_stable(episode):
        return False, True
    if not await episode_revision_is_published(episode):
        return False, True

    summary_due = False
    summary_terminal = True
    eligibility = await episode_summary_eligibility(episode)
    if eligibility.eligible and eligibility.scope_hash:
        materialized = (
            bool(episode.detailed_summary)
            and episode.detailed_summary_scope_hash == eligibility.scope_hash
            and episode.detailed_summary_revision == int(episode.revision)
        )
        summary_terminal = materialized
        summary_due = not materialized and not await _summary_lease_is_active(
            episode, eligibility.scope_hash, now=now
        )

    completion_due = False
    completion_terminal = True
    if episode.status == "settled":
        completion = await EpisodeDispatchLatch.get_pymongo_collection().find_one(
            {
                "user_id": episode.user_id,
                "episode_key": episode.episode_key,
                "event_type": CONVERSATION_COMPLETE,
            },
            {"_id": 1},
        )
        completion_due = completion is None
        completion_terminal = completion is not None
    return (
        summary_due or completion_due,
        summary_terminal and completion_terminal,
    )


async def mark_episode_publications_dispatch_pending(
    user_id: str, episode_refs: list[tuple[str, int]]
) -> int:
    """Reopen committed current snapshot owners before status creates new work."""

    refs = sorted(set(episode_refs))
    if not refs:
        return 0
    current_days = await TimelineDay.find(
        {
            "user_id": user_id,
            "snapshot_state": {"$in": sorted(SUMMARY_DAY_STATES)},
            "pending_publication_id": {"$in": [None, ""]},
            "$or": [
                {
                    "current_snapshot.episode_revisions": {
                        "$elemMatch": {
                            "episode_key": episode_key,
                            "revision": revision,
                        }
                    }
                }
                for episode_key, revision in refs
            ],
        }
    ).to_list()
    snapshot_ids_by_ref: dict[tuple[str, int], set[str]] = {ref: set() for ref in refs}
    for day in current_days:
        if day.current_snapshot is None or day.current_snapshot_id is None:
            continue
        day_refs = {
            (ref.episode_key, int(ref.revision))
            for ref in day.current_snapshot.episode_revisions
        }
        for ref in day_refs.intersection(snapshot_ids_by_ref):
            snapshot_ids_by_ref[ref].add(day.current_snapshot_id)
    owner_clauses = [
        {
            "affected_days": {
                "$elemMatch": {
                    "resulting_snapshot.snapshot_id": {"$in": sorted(snapshot_ids)},
                    "resulting_snapshot.episode_revisions": {
                        "$elemMatch": {
                            "episode_key": episode_key,
                            "revision": revision,
                        }
                    },
                }
            }
        }
        for (episode_key, revision), snapshot_ids in snapshot_ids_by_ref.items()
        if snapshot_ids
    ]
    if not owner_clauses:
        return 0
    result = await TimelinePublicationJournal.get_pymongo_collection().update_many(
        {
            "user_id": user_id,
            "status": "committed",
            "$or": owner_clauses,
        },
        {
            "$set": {
                "dispatch_pending": True,
                "dispatch_completed_at": None,
            }
        },
    )
    return int(result.modified_count)


async def dispatch_classified_episodes(
    user_id: str, episode_ids: list[str]
) -> dict[str, list[str]]:
    """Dispatch derived work only for stable exact episode revisions.

    Timeline memory is intentionally absent here. One finalized day snapshot owns the
    reviewable vault proposal; neither a raw Conversation nor a provisional Episode may
    mutate durable memory.
    """

    if not episode_ids:
        return {"summaries": [], "events": []}

    episodes = await TimelineEpisode.find(
        {"episode_id": {"$in": list(episode_ids)}, "user_id": user_id}
    ).to_list()

    summaries_dispatched: list[str] = []
    events_dispatched: list[str] = []
    for episode in episodes:
        if not episode.conversational:
            if episode.status == "settled":
                await _retype_media_source_record(episode)
            continue
        if not episode_structure_is_stable(episode):
            continue
        if not await episode_revision_is_published(episode):
            continue

        conversations = await _related_conversations(episode)
        if await enqueue_episode_detailed_summary(episode):
            summaries_dispatched.append(episode.episode_key)
            logger.info(
                "📌 Dispatched %s for classified episode %s (revision %s, status=%s)",
                EPISODE_DETAILED_SUMMARY,
                episode.episode_key,
                episode.revision,
                episode.status,
            )
        if episode.status == "settled" and await _claim_and_fire(
            episode, CONVERSATION_COMPLETE, _fire_plugins, conversations
        ):
            events_dispatched.append(episode.episode_key)
            logger.info(
                "📌 Dispatched %s for settled episode %s (revision %s)",
                CONVERSATION_COMPLETE,
                episode.episode_key,
                episode.revision,
            )

    return {"summaries": summaries_dispatched, "events": events_dispatched}


async def dispatch_ready_episodes(limit: int = 200) -> dict[str, int]:
    """Recover due work from the bounded committed-publication queue."""

    batch_size = max(1, limit)
    journal_rows = (
        await TimelinePublicationJournal.get_pymongo_collection()
        .find({"status": "committed", "dispatch_pending": True})
        .sort("committed_at", 1)
        .limit(batch_size)
        .to_list(length=batch_size)
    )
    journals = [TimelinePublicationJournal.model_validate(row) for row in journal_rows]
    due_count = 0
    dispatched_keys: set[str] = set()
    for journal in journals:
        episodes = await _current_journal_episodes(journal)
        due: list[TimelineEpisode] = []
        for episode in episodes:
            due_now, _terminal = await _episode_dispatch_state(episode, now=utcnow())
            if due_now:
                due.append(episode)
        due_count += len(due)
        for episode in due:
            outcome = await dispatch_classified_episodes(
                episode.user_id, [episode.episode_id]
            )
            dispatched_keys.update(outcome["summaries"])
            dispatched_keys.update(outcome["events"])

        refreshed = await _current_journal_episodes(journal)
        terminal = True
        for episode in refreshed:
            _due_now, episode_terminal = await _episode_dispatch_state(
                episode, now=utcnow()
            )
            terminal = terminal and episode_terminal
        if terminal:
            await TimelinePublicationJournal.get_pymongo_collection().update_one(
                {
                    "_id": journal.id,
                    "status": "committed",
                    "dispatch_pending": True,
                },
                {
                    "$set": {
                        "dispatch_pending": False,
                        "dispatch_completed_at": utcnow(),
                    }
                },
            )

    if journals or dispatched_keys:
        logger.info(
            "📌 Classified-episode recovery: %d pending journals, %d due, "
            "%d dispatched",
            len(journals),
            due_count,
            len(dispatched_keys),
        )
    return {"unlatched": due_count, "dispatched": len(dispatched_keys)}


@async_job(redis=True, beanie=True)
async def finalize_conversation_close_job(
    conversation_id: str,
    client_id: str,
    user_id: str,
    end_reason: Optional[str] = None,
    trigger: str = "",
    *,
    redis_client=None,
) -> dict[str, Any]:
    """Terminal close-path job for rolling users: settle state, dispatch nothing.

    ``dispatch_conversation_complete_event_job`` does two separable things: it owns the
    conversation's final ``end_reason``/``completed_at``/``processing_status``, and it
    fires ``conversation.complete``. Under rolling reconciliation only the first still
    belongs to the close path. This job is that half; it disappears when the day
    pipeline is removed at cutover and the remaining finalizer stops dispatching.
    """

    # Imported here to break the import cycle with the workers package, which imports
    # the queue controller and hence this package's dirty-range trigger.
    from backend.workers.conversation_jobs import request_conversation_context_jobs

    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    needs_save = False
    if end_reason and conversation.end_reason is None:
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.error(
                "⚠️ %s is not a Conversation.EndReason (conversation %s); "
                "storing UNKNOWN",
                end_reason,
                conversation_id,
            )
            conversation.end_reason = Conversation.EndReason.UNKNOWN
        needs_save = True

    if conversation.completed_at is None:
        conversation.completed_at = utcnow()
        needs_save = True

    if conversation.apply_status(settled=True):
        needs_save = True

    if needs_save:
        await conversation.save()

    try:
        await request_conversation_context_jobs(conversation)
    except Exception as exc:
        logger.warning(
            "Failed to request device context for conversation %s: %s",
            conversation_id,
            exc,
        )

    publish_sse_event(
        user_id,
        "conversation.completed",
        {
            "conversation_id": conversation_id,
            "end_reason": (
                conversation.end_reason.value if conversation.end_reason else None
            ),
            "trigger": trigger,
        },
    )
    logger.info(
        "🏁 Finalized conversation %s status=%s (rolling: events fire on episode "
        "settlement)",
        conversation_id[:12],
        conversation.processing_status,
    )
    return {"success": True, "conversation_id": conversation_id, "dispatched": False}
