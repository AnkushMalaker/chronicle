"""Record a settled local day of timeline episodes into the memory vault.

The conversation is the wrong memory unit for capture evidence. Continuous audio is
persisted independently as immutable capture chunks; detected Conversations and bounded
processing windows are mutable claims over that evidence, not its temporal identity. A
timeline episode carries the semantic bounds, so the day of episodes is what gets
remembered and the underlying capture ranges remain the cited artifacts.

Writing happens once per (user, local_date), for a day that has stopped changing. Every
analysis run regenerates a whole day from scratch and does so non-deterministically, so
writing on publish would flap: the same stretch of the day would be recorded again under
different boundaries on the next tick. ``TimelineDay.memory_state`` is the latch.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

from advanced_omi_backend.models.timeline import TimelineDay, TimelineEpisode, utcnow
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    UpdateStrategy,
    memory_provenance,
)
from advanced_omi_backend.services.memory.base import DayWriteOutcome

from .executor import settings_dict
from .timezone import canonical_timezone

logger = logging.getLogger(__name__)

# Defaults for the ``timeline.memory`` config block.
_DEFAULT_LOOKBACK_DAYS = 2
_DEFAULT_MAX_DAYS_PER_RUN = 2
_DEFAULT_SETTLE_MINUTES = 60
_DEFAULT_CLAIM_TIMEOUT_MINUTES = 120
_DEFAULT_MAX_ATTEMPTS = 3
# ~15K tokens, the size audited memory prompts already reach successfully. Larger day
# bundles are trimmed rather than truncated silently.
_DEFAULT_MAX_DIGEST_CHARS = 60000

_SALIENCE_RANK = {"background": 0, "routine": 1, "notable": 2, "highlight": 3}

# A day in one of these states is done with; the settled-day scan must not pick it up
# again. Named once because a scan that forgets a member silently re-runs the agent
# over days it already settled.
_TERMINAL_MEMORY_STATES = ("written", "partial", "skipped", "no_changes")


def memory_settings() -> dict[str, Any]:
    block = settings_dict().get("memory") or {}
    return block if isinstance(block, dict) else {}


def _setting(name: str, default: int) -> int:
    try:
        return int(memory_settings().get(name, default))
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime) -> datetime:
    """Mongo returns naive datetimes; they are UTC, not node-local.

    Without this an episode's clock is shifted by the host's offset, and comparing a
    stored timestamp against ``utcnow()`` raises outright.
    """

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clock(episode: TimelineEpisode, zone: ZoneInfo) -> str:
    started = _as_utc(episode.started_at).astimezone(zone).strftime("%H:%M")
    ended = _as_utc(episode.ended_at).astimezone(zone).strftime("%H:%M")
    return f"{started}–{ended}"


def render_episode(
    episode: TimelineEpisode,
    zone: ZoneInfo,
) -> str:
    """Render one episode for the write agent.

    Assertions keep their ``role`` and ``confidence`` because those are what stop media
    dialogue, application output, and assistant text from being recorded as facts about
    the user. Raw evidence is deliberately excluded: the vault receives the timeline's
    bounded interpretation, while transcripts remain in MongoDB as source evidence.
    """

    lines = [
        f"### {_clock(episode, zone)} · {episode.kind} · {episode.salience}",
        f"title: {episode.title}",
    ]
    if episode.summary:
        lines.append(f"summary: {episode.summary}")
    if episode.entities:
        lines.append(f"entities: {', '.join(episode.entities)}")
    if episode.attributes:
        rendered = "; ".join(
            f"{key}={value}" for key, value in sorted(episode.attributes.items())
        )
        lines.append(f"attributes: {rendered}")
    if episode.assertions:
        lines.append("assertions:")
        lines.extend(
            f"- [{assertion.role} · confidence {assertion.confidence:.2f}] "
            f"{assertion.claim}"
            for assertion in episode.assertions
        )
    return "\n".join(lines)


def build_day_digest(
    episodes: list[TimelineEpisode],
    local_date: date,
    timezone_name: str,
    max_chars: int | None = None,
) -> tuple[str, list[str]]:
    """Render a day of episodes within a character budget.

    Episodes are shed lowest-salience-first when summaries overflow. Conversational
    episodes are never shed. Raw evidence is never part of this digest: it remains in
    the corpus and timeline records instead of being copied into the memory vault.
    """

    budget = (
        max_chars
        if max_chars is not None
        else _setting("max_digest_chars", _DEFAULT_MAX_DIGEST_CHARS)
    )
    zone = ZoneInfo(timezone_name)
    ordered = sorted(episodes, key=lambda item: (item.started_at, item.ended_at))
    rendered = {
        episode.episode_id: render_episode(episode, zone) for episode in ordered
    }

    header = (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(ordered)} episode(s)."
    )
    keep = {episode.episode_id for episode in ordered}

    def total() -> int:
        return len(header) + sum(len(rendered[episode_id]) + 2 for episode_id in keep)

    dropped: list[str] = []

    # Shed only low-salience non-conversational summaries. Conversations remain
    # represented by their bounded episode summary, never by copied dialogue.
    droppable = sorted(
        (episode for episode in ordered if not episode.conversational),
        key=lambda item: (
            _SALIENCE_RANK.get(item.salience, 1),
            (item.ended_at - item.started_at),
        ),
    )
    for episode in droppable:
        if total() <= budget:
            break
        keep.discard(episode.episode_id)
        dropped.append(episode.title)

    # A header claiming 13 episodes above a body holding 4 is a digest that lies to the
    # model, and both a 27B local model and DeepSeek V4 Pro duly reported having covered
    # "all four episodes" of that thirteen-episode day. Say what is actually here.
    if len(keep) != len(ordered):
        header = (
            f"Local day {local_date.isoformat()} ({timezone_name}), "
            f"{len(keep)} of {len(ordered)} episode(s) — "
            f"{len(ordered) - len(keep)} omitted to fit; the day was longer than this."
        )

    body = "\n\n".join(
        rendered[episode.episode_id]
        for episode in ordered
        if episode.episode_id in keep
    )
    return f"{header}\n\n{body}", dropped


def build_day_index_digest(
    episodes: list[TimelineEpisode], local_date: date, timezone_name: str
) -> str:
    """Render every active episode for Chronicle's concise Daily index.

    This source is deliberately separate from the bounded semantic digest: shedding a
    low-salience summary to fit the model prompt must never remove that episode's exact
    range from the deterministic index.
    """

    zone = ZoneInfo(timezone_name)
    ordered = sorted(episodes, key=lambda item: (item.started_at, item.ended_at))
    body = "\n\n".join(
        f"### {_clock(episode, zone)} · {episode.kind} · {episode.salience}\n"
        f"title: {episode.title}"
        for episode in ordered
    )
    return (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(ordered)} episode(s).\n\n{body}"
    )


def _claim_query(
    day: TimelineDay, claim_timeout_minutes: int, max_attempts: int
) -> dict[str, Any]:
    """Match the day only while it is genuinely available to claim.

    A ``claimed`` day whose claim has aged out is reclaimable: the process holding it
    died. ``written`` and ``skipped`` are terminal and never match.
    """

    stale_before = utcnow() - timedelta(minutes=claim_timeout_minutes)
    return {
        "user_id": day.user_id,
        "local_date": datetime.combine(day.local_date, datetime.min.time()),
        "timezone": day.timezone,
        # A caller holding a stale day object must not claim the newly published run
        # under the old run id.
        "active_run_id": day.active_run_id,
        # $not/$gte, not $lt: a day analysed before this field existed has no
        # memory_attempts at all, and $lt never matches a missing field.
        "memory_attempts": {"$not": {"$gte": max_attempts}},
        "$or": [
            {"memory_state": {"$in": ["", None]}},
            {"memory_state": "claimed", "memory_claimed_at": {"$lt": stale_before}},
        ],
    }


async def _settled_days(user: User, timezone_name: str) -> list[TimelineDay]:
    """Past days that have an analysis and no vault record yet.

    Bounded by ``lookback_days`` so enabling this on an existing deployment does not
    walk the entire history and rewrite months of vault at once.
    """

    lookback = _setting("lookback_days", _DEFAULT_LOOKBACK_DAYS)
    settle_minutes = _setting("settle_minutes", _DEFAULT_SETTLE_MINUTES)
    max_attempts = _setting("max_attempts", _DEFAULT_MAX_ATTEMPTS)
    zone = ZoneInfo(timezone_name)
    today = datetime.now(timezone.utc).astimezone(zone).date()
    earliest = today - timedelta(days=lookback)
    settled_before = utcnow() - timedelta(minutes=settle_minutes)

    days = await TimelineDay.find(
        {
            "user_id": str(user.id),
            "timezone": timezone_name,
            "local_date": {
                "$gte": datetime.combine(earliest, datetime.min.time()),
                "$lt": datetime.combine(today, datetime.min.time()),
            },
            "active_run_id": {"$nin": [None, ""]},
            "memory_state": {"$nin": list(_TERMINAL_MEMORY_STATES)},
            "memory_attempts": {"$not": {"$gte": max_attempts}},
        }
    ).to_list()
    logger.info(
        "🗓️ settled-day scan user=%s tz=%s range=%s..%s matched=%d",
        user.id,
        timezone_name,
        earliest,
        today,
        len(days),
    )
    # A re-analysis that landed moments ago means the day is still moving; give it time
    # to stop before spending an agent run on it.
    return sorted(
        (
            day
            for day in days
            if _as_utc(day.active_run_created_at or day.revised_at) <= settled_before
        ),
        key=lambda item: item.local_date,
        reverse=True,
    )


async def _write_day(day: TimelineDay) -> str:
    """Write one claimed day.

    Returns ``written``, ``no_changes``, ``partial``, ``skipped``, or ``failed``.
    """

    episodes = await TimelineEpisode.find(
        TimelineEpisode.run_id == day.active_run_id,
        TimelineEpisode.user_id == day.user_id,
    ).to_list()
    if not episodes:
        logger.info(
            "🗓️ Timeline day %s for user %s has no episodes — nothing to record",
            day.local_date,
            day.user_id,
        )
        return "skipped"

    digest, dropped = build_day_digest(episodes, day.local_date, day.timezone)
    index_digest = build_day_index_digest(episodes, day.local_date, day.timezone)
    if dropped:
        logger.warning(
            "🗓️ Day %s digest exceeded its budget; dropped %d low-salience "
            "episode(s) from the vault write: %s",
            day.local_date,
            len(dropped),
            "; ".join(dropped),
        )

    memory_service = get_memory_service()
    with memory_provenance(MemoryCause.DAY_EPISODES.value, UpdateStrategy.FULL.value):
        outcome, touched = await memory_service.add_day_memory(
            digest,
            day.local_date.isoformat(),
            day.user_id,
            day_index_digest=index_digest,
            source_date=datetime.combine(
                day.local_date, datetime.min.time(), tzinfo=ZoneInfo(day.timezone)
            ).isoformat(),
        )
    if outcome is DayWriteOutcome.FAILED:
        return "failed"

    async def record_paths(paths: list[str], state: str = "written") -> None:
        await TimelineEpisode.get_pymongo_collection().update_many(
            {"episode_id": {"$in": [episode.episode_id for episode in episodes]}},
            {"$set": {"memory_state": state, "vault_paths": paths}},
        )

    if outcome is DayWriteOutcome.PARTIAL:
        # Terminal, and deliberately not `written`: the audited mutations are kept, but
        # the run was cut off and may never have reached its People/Topic edits. Not
        # retried either — truncation is a property of the model's round limit, not of
        # this day, so the next two attempts would reach the same place and then settle
        # it as `skipped`, which claims there was nothing to record.
        paths = list(dict.fromkeys(touched))
        await record_paths(paths, state="partial")
        logger.error(
            "🗓️ Day %s for user %s recorded only partially: %d episode(s), "
            "%d note(s) touched — not retrying, republish the day to rewrite it",
            day.local_date,
            day.user_id,
            len(episodes),
            len(paths),
        )
        return "partial"

    if not touched:
        # The agent completed and chose to record nothing. Terminal, not a failure:
        # retrying only re-reaches the same judgement.
        logger.info(
            "🗓️ Day %s for user %s needed no vault change (%d episode(s))",
            day.local_date,
            day.user_id,
            len(episodes),
        )
        await record_paths([])
        return "no_changes"

    paths = list(dict.fromkeys(touched))
    await record_paths(paths)
    logger.info(
        "🗓️ Recorded day %s for user %s: %d episode(s), %d note(s) touched",
        day.local_date,
        day.user_id,
        len(episodes),
        len(paths),
    )
    return "written"


async def write_day_memory(day: TimelineDay) -> str:
    """Claim one day and record it, settling ``memory_state`` however it ends.

    The claim is what makes this safe to call directly — a rebuild replaying a range of
    days and the cron scanning settled days can both reach the same day, and only one of
    them may spend an agent run on it. Returns the outcome, or ``"busy"`` when another
    holder has it. Returns ``"superseded"`` when a newer analysis publishes while the
    agent is writing; that newer generation remains explicitly unwritten for the next
    settled-day pass.
    """

    max_attempts = _setting("max_attempts", _DEFAULT_MAX_ATTEMPTS)
    claim_timeout = _setting("claim_timeout_minutes", _DEFAULT_CLAIM_TIMEOUT_MINUTES)
    collection = TimelineDay.get_pymongo_collection()
    claimed = await collection.find_one_and_update(
        _claim_query(day, claim_timeout, max_attempts),
        {
            "$set": {
                "memory_state": "claimed",
                "memory_claimed_at": utcnow(),
                "memory_run_id": day.active_run_id,
            },
            "$inc": {"memory_attempts": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        return "busy"

    attempts = claimed.get("memory_attempts", 1)
    try:
        outcome = await _write_day(day)
        error = None
    except Exception as exc:  # noqa: BLE001 - a bad day must not stop the rest
        outcome = "failed"
        error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.error(
            "🗓️ Recording day %s for user %s failed",
            day.local_date,
            day.user_id,
            exc_info=True,
        )

    if outcome == "failed":
        # Release for retry until the attempt budget is spent, then settle into
        # `skipped` with the diagnostic rather than retrying forever.
        exhausted = attempts >= max_attempts
        await collection.update_one(
            {
                "_id": claimed["_id"],
                "active_run_id": claimed.get("memory_run_id"),
                "memory_run_id": claimed.get("memory_run_id"),
                "memory_state": "claimed",
            },
            {
                "$set": {
                    "memory_state": "skipped" if exhausted else "",
                    "memory_error": error or "day memory write failed",
                }
            },
        )
        if exhausted:
            logger.error(
                "🗓️ Day %s for user %s exhausted %d attempts — not retrying",
                day.local_date,
                day.user_id,
                max_attempts,
            )
    else:
        settled = await collection.update_one(
            {
                "_id": claimed["_id"],
                "active_run_id": claimed.get("memory_run_id"),
                "memory_run_id": claimed.get("memory_run_id"),
                "memory_state": "claimed",
            },
            {
                "$set": {
                    "memory_state": outcome,
                    "memory_written_at": utcnow(),
                    "memory_error": None,
                }
            },
        )
        if settled.modified_count != 1:
            logger.warning(
                "🗓️ Day %s changed active run while memory was being written; "
                "leaving the newer generation unwritten for reconciliation",
                day.local_date,
            )
            return "superseded"
    return outcome


async def process_episode_memory() -> dict[str, int]:
    """Record every settled day that has episodes but no vault entry yet."""

    totals = {
        "considered": 0,
        "written": 0,
        "no_changes": 0,
        "skipped": 0,
        "failed": 0,
        "superseded": 0,
    }
    max_days = _setting("max_days_per_run", _DEFAULT_MAX_DAYS_PER_RUN)

    users = await User.find({"timezone": {"$nin": [None, ""]}}).to_list()
    processed = 0
    for user in users:
        if processed >= max_days:
            break
        timezone_name = canonical_timezone(user.timezone)
        for day in await _settled_days(user, timezone_name):
            if processed >= max_days:
                break
            totals["considered"] += 1
            outcome = await write_day_memory(day)
            if outcome == "busy":
                continue  # another worker holds it
            processed += 1
            totals[outcome] += 1
    return totals
