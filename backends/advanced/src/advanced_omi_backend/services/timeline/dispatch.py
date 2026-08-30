"""Classification-gated dispatch of user-facing conversation events.

Under rolling reconciliation a recording closing is a scheduling signal, not proof
that a conversation happened. The close path still runs every evidence producer and
still writes the provisional Conversation source record. The per-conversation memory
write fires from the **first non-open conversational Episode revision**; the
user-facing ``conversation.complete`` plugin event waits for ``settled``. Completion
is latched per Episode; memory is latched per source Conversation because a Timeline
split can make two Episodes reference the same source. ``provisional`` means the
boundary may still revise; Timeline has already made the semantic
conversation-versus-media classification needed to gate memory.

See docs/backend/rolling-reconciliation.md, "Classification-gated event dispatch".

Failure semantics: each latch is claimed *before* its side effect, so two concurrent
publishes cannot both dispatch it. If that effect raises, only its latch is deleted so
a later classification/recovery pass re-fires it. That makes delivery at-least-once
rather than at-most-once, which is the right trade here: the downstream work (plugin
events, the memory write keyed on the conversation) is idempotent or re-runnable,
while a silently dropped dispatch means an episode the user never hears about and no
record of why.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.timeline import (
    EpisodeDispatchLatch,
    TimelineEpisode,
    utcnow,
)
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.memory.visibility import conversation_scope_filter
from advanced_omi_backend.services.plugin_service import dispatch_plugin_event
from advanced_omi_backend.services.sse_publisher import publish_sse_event

from .recording_refs import episode_conversation_ids, resolve_live_recordings

logger = logging.getLogger(__name__)

CONVERSATION_COMPLETE = PluginEvent.CONVERSATION_COMPLETE.value
MEMORY_EXTRACTION = "memory.extraction"
DISPATCHABLE_STATUSES = ("provisional", "settled")


_users_col = None


def _get_users_col():
    global _users_col
    if _users_col is None:
        # Lazy import: standalone sync pymongo client, deliberately not Beanie/Motor —
        # same constraint as workers/job_callbacks.py (see below).
        from pymongo import MongoClient

        uri = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
        db_name = os.getenv("MONGODB_DATABASE", "chronicle")
        _users_col = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name]["users"]
    return _users_col


def active_pipeline_sync(user_id: str) -> str:
    """Read ``User.active_timeline_pipeline`` from synchronous code.

    ``start_post_conversation_jobs`` is sync and runs inside a running event loop, so
    it can neither await Beanie nor block on it. This is one projected ``_id`` lookup
    per conversation close, through a standalone sync client for the same reason
    ``workers/job_callbacks.py`` uses one: the caller's process may not have Beanie
    initialized. Any failure answers ``"day"`` — the unchanged existing behaviour.
    """

    try:
        document = _get_users_col().find_one(
            {"_id": ObjectId(user_id)}, {"active_timeline_pipeline": 1}
        )
    except Exception:
        logger.warning(
            "Could not read the active timeline pipeline for %s; assuming 'day'",
            user_id,
            exc_info=True,
        )
        return "day"
    if not document:
        return "day"
    return document.get("active_timeline_pipeline") or "day"


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


async def _fire_memory(
    _episode: TimelineEpisode, conversations: list[Conversation]
) -> None:
    """Enqueue per-conversation memory writes after Timeline classification."""

    # Imported here to break the import cycle: workers.memory_jobs imports the queue
    # controller, which imports the timeline dirty-range trigger from this package.
    from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing

    for conversation in conversations:
        enqueue_memory_processing(conversation.conversation_id)


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


async def dispatch_classified_episodes(
    user_id: str, episode_ids: list[str]
) -> dict[str, list[str]]:
    """Dispatch memory at classification and plugins only at settlement.

    The two effects have separate latches. A provisional classification can therefore
    make memory available without sending a premature summary email, and later
    settlement can publish ``conversation.complete`` exactly once.
    """

    if not episode_ids:
        return {"memory": [], "events": []}

    episodes = await TimelineEpisode.find(
        {"episode_id": {"$in": list(episode_ids)}, "user_id": user_id}
    ).to_list()

    memory_dispatched: list[str] = []
    events_dispatched: list[str] = []
    for episode in episodes:
        if episode.status not in DISPATCHABLE_STATUSES:
            continue
        if not episode.conversational:
            if episode.status == "settled":
                await _retype_media_source_record(episode)
            continue

        conversations = await _related_conversations(episode)
        memory_fired = False
        for conversation in conversations:
            if await _claim_and_fire(
                episode,
                MEMORY_EXTRACTION,
                _fire_memory,
                [conversation],
                latch_key=f"conversation:{conversation.conversation_id}",
            ):
                memory_fired = True
        if memory_fired:
            memory_dispatched.append(episode.episode_key)
            logger.info(
                "📌 Dispatched %s for classified episode %s (revision %s, status=%s)",
                MEMORY_EXTRACTION,
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

    return {"memory": memory_dispatched, "events": events_dispatched}


async def dispatch_ready_episodes(limit: int = 200) -> dict[str, int]:
    """Recover classified conversational Episodes missing a required dispatch latch.

    Publishing normally dispatches immediately. This bounded scan covers a crash
    between publish and dispatch, rows created before this policy existed, and a
    temporarily unavailable queue or plugin router. Provisional episodes require the
    conversation-scoped memory latch for every related source; settled episodes also
    require their episode-scoped completion latch. The aggregation excludes fully
    dispatched rows before applying ``limit``, so old successful rows cannot starve
    new work.
    """

    collection = TimelineEpisode.get_pymongo_collection()
    rows = await collection.aggregate(
        [
            {
                "$match": {
                    "pipeline": "rolling",
                    "status": {"$in": list(DISPATCHABLE_STATUSES)},
                    "conversational": True,
                }
            },
            {
                "$set": {
                    "source_conversation_ids": {
                        "$setUnion": [
                            {"$ifNull": ["$related_conversation_ids", []]},
                            {
                                "$reduce": {
                                    "input": {"$ifNull": ["$audio_ranges", []]},
                                    "initialValue": [],
                                    "in": {
                                        "$setUnion": [
                                            "$$value",
                                            {
                                                "$ifNull": [
                                                    "$$this.conversation_ids",
                                                    [],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                        ]
                    }
                }
            },
            {
                "$lookup": {
                    "from": "conversations",
                    "localField": "source_conversation_ids",
                    "foreignField": "conversation_id",
                    "as": "related_conversations",
                }
            },
            {
                "$set": {
                    "memory_latch_keys": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$related_conversations",
                                    "as": "conversation",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$ne": [
                                                    "$$conversation.deleted",
                                                    True,
                                                ]
                                            },
                                            {
                                                "$ne": [
                                                    "$$conversation.memory_excluded",
                                                    True,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "conversation",
                            "in": {
                                "$concat": [
                                    "conversation:",
                                    "$$conversation.conversation_id",
                                ]
                            },
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "episode_dispatch_latches",
                    "localField": "memory_latch_keys",
                    "foreignField": "episode_key",
                    "as": "memory_latches",
                }
            },
            {
                "$lookup": {
                    "from": "episode_dispatch_latches",
                    "let": {"key": "$episode_key"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$episode_key", "$$key"]},
                                        {
                                            "$eq": [
                                                "$event_type",
                                                CONVERSATION_COMPLETE,
                                            ]
                                        },
                                    ]
                                }
                            }
                        },
                        {"$limit": 1},
                    ],
                    "as": "completion_latches",
                }
            },
            {
                "$match": {
                    "$expr": {
                        "$or": [
                            {
                                "$gt": [
                                    {
                                        "$size": {
                                            "$setDifference": [
                                                "$memory_latch_keys",
                                                "$memory_latches.episode_key",
                                            ]
                                        }
                                    },
                                    0,
                                ]
                            },
                            {
                                "$and": [
                                    {"$eq": ["$status", "settled"]},
                                    {"$eq": [{"$size": "$completion_latches"}, 0]},
                                ]
                            },
                        ]
                    }
                }
            },
            {"$sort": {"ended_at": 1, "episode_id": 1}},
            {"$limit": limit},
            {"$project": {"_id": 0, "episode_id": 1, "user_id": 1}},
        ]
    ).to_list(length=limit)

    by_user: dict[str, list[str]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row["episode_id"])

    dispatched_keys: set[str] = set()
    for user_id, episode_ids in by_user.items():
        outcome = await dispatch_classified_episodes(user_id, episode_ids)
        dispatched_keys.update(outcome["memory"])
        dispatched_keys.update(outcome["events"])

    if rows or dispatched_keys:
        logger.info(
            "📌 Classified-episode recovery: %d unlatched, %d dispatched",
            len(rows),
            len(dispatched_keys),
        )
    return {"unlatched": len(rows), "dispatched": len(dispatched_keys)}


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
    from advanced_omi_backend.workers.conversation_jobs import (
        request_conversation_context_jobs,
    )

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
