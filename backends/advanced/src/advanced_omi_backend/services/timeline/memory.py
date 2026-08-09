"""Record a settled local day of timeline episodes into the memory vault.

The conversation is the wrong memory unit for capture evidence. Continuous ScreenPipe
audio is assembled into bounded compute spans — up to 30 minutes, or two hours when the
collector supplied a meeting interval — so a 45-minute standup without meeting detection
is already two recordings. A timeline episode carries the semantic bounds instead, so the
day of episodes is what gets remembered and the recordings underneath are the artifacts
it cites.

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

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import TimelineDay, TimelineEpisode, utcnow
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    UpdateStrategy,
    memory_provenance,
)
from advanced_omi_backend.workers.memory_jobs import build_memory_transcript

from .episode_notes import write_episode_notes
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
    transcripts: dict[str, str],
) -> str:
    """Render one episode for the write agent.

    Assertions keep their ``role`` and ``confidence`` because those are what stop media
    dialogue, application output, and assistant text from being recorded as facts about
    the user. Transcripts are attached only for conversational episodes.
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
    if episode.conversational:
        cited = [
            transcripts[conversation_id]
            for conversation_id in sorted(_cited_conversation_ids(episode))
            if transcripts.get(conversation_id)
        ]
        if cited:
            lines.append("transcript (speaker-labelled):")
            lines.extend(cited)
        else:
            lines.append(
                "transcript: unavailable — record only what the assertions support"
            )
    return "\n".join(lines)


def _cited_conversation_ids(episode: TimelineEpisode) -> set[str]:
    cited = {str(item) for item in episode.related_conversation_ids if item}
    for ref in episode.evidence_refs:
        conversation_id = ref.metadata.get("conversation_id")
        if conversation_id:
            cited.add(str(conversation_id))
    return cited


async def _episode_transcripts(episodes: list[TimelineEpisode]) -> dict[str, str]:
    """Speaker-labelled transcripts for every conversational episode's recordings."""

    wanted: set[str] = set()
    for episode in episodes:
        if episode.conversational:
            wanted |= _cited_conversation_ids(episode)
    if not wanted:
        return {}
    conversations = await Conversation.find(
        {"conversation_id": {"$in": sorted(wanted)}}
    ).to_list()
    transcripts: dict[str, str] = {}
    for conversation in conversations:
        # Reused from the conversation memory path so provider window-overlap trimming
        # and the raw-transcript fallback behave identically here.
        text, _ = build_memory_transcript(
            conversation.segments, conversation.transcript
        )
        if text.strip():
            transcripts[conversation.conversation_id] = text.strip()
    return transcripts


def build_day_digest(
    episodes: list[TimelineEpisode],
    local_date: date,
    timezone_name: str,
    transcripts: dict[str, str],
    max_chars: int | None = None,
) -> tuple[str, list[str]]:
    """Render a day of episodes within a character budget.

    Transcripts are trimmed first, then — only if the summaries alone still overflow —
    episodes are shed lowest-salience-first. Conversational episodes are never shed;
    they carry the day's actual speech. Returns the digest and what was given up, so a
    silently shortened day cannot read to the caller as a complete one.
    """

    budget = (
        max_chars
        if max_chars is not None
        else _setting("max_digest_chars", _DEFAULT_MAX_DIGEST_CHARS)
    )
    zone = ZoneInfo(timezone_name)
    ordered = sorted(episodes, key=lambda item: (item.started_at, item.ended_at))
    rendered = {
        episode.episode_id: render_episode(episode, zone, transcripts)
        for episode in ordered
    }

    header = (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(ordered)} episode(s)."
    )
    keep = {episode.episode_id for episode in ordered}

    def total() -> int:
        return len(header) + sum(len(rendered[episode_id]) + 2 for episode_id in keep)

    dropped: list[str] = []

    # Trim transcripts before dropping episodes. An episode summary costs a few hundred
    # characters and a transcript tens of thousands, so shedding episodes to make room
    # for transcripts discards most of the day to save almost nothing — one measured day
    # dropped 9 of 13 episodes and then had to trim the transcripts anyway, leaving the
    # agent summarising "all four episodes" of a thirteen-episode day. Losing the tail of
    # a conversation is recoverable; losing the fact that an episode happened is not.
    bare = {
        episode.episode_id: render_episode(episode, zone, {}) for episode in ordered
    }

    def overhead() -> int:
        return len(header) + sum(len(bare[episode_id]) + 2 for episode_id in keep)

    cited = [
        conversation_id
        for episode in ordered
        for conversation_id in sorted(_cited_conversation_ids(episode))
        if transcripts.get(conversation_id)
    ]
    if cited and total() > budget:
        share = max(0, budget - overhead()) // len(cited)
        trimmed = dict(transcripts)
        for conversation_id in cited:
            text = transcripts[conversation_id]
            if len(text) > share:
                trimmed[conversation_id] = (
                    text[:share].rstrip() + "\n[transcript trimmed to fit]"
                )
        for episode in ordered:
            rendered[episode.episode_id] = render_episode(episode, zone, trimmed)
        dropped.append(
            f"transcripts trimmed to {share} chars across {len(cited)} recording(s)"
        )

    # Only the summaries themselves are left. If they still overflow, shed the
    # lowest-salience non-conversational episodes; conversational ones carry the day's
    # actual speech and are never dropped.
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
            "memory_state": {"$nin": ["written", "skipped", "no_changes"]},
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


def _write_episode_notes(
    memory_service: Any,
    day: TimelineDay,
    episodes: list[TimelineEpisode],
    transcripts: dict[str, str],
) -> list[str]:
    """Record each decided bound, tolerating a provider that has no vault."""

    vault = getattr(memory_service, "vault", None)
    if vault is None:
        return []
    return write_episode_notes(
        vault.user_root(day.user_id),
        episodes,
        day.timezone,
        transcripts,
        day_note_name=day.local_date.isoformat(),
    )


async def _write_day(day: TimelineDay) -> str:
    """Write one claimed day. Returns ``written``, ``skipped``, or ``failed``."""

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

    transcripts = await _episode_transcripts(episodes)
    digest, dropped = build_day_digest(
        episodes, day.local_date, day.timezone, transcripts
    )
    if dropped:
        logger.warning(
            "🗓️ Day %s digest exceeded its budget; dropped %d low-salience "
            "episode(s) from the vault write: %s",
            day.local_date,
            len(dropped),
            "; ".join(dropped),
        )

    memory_service = get_memory_service()
    # Written before the agent runs, and kept even if it fails: the record of what
    # happened in a decided bound must not depend on a model completing. It is also the
    # only place a long transcript survives whole, since the digest above trims them.
    episode_notes = _write_episode_notes(memory_service, day, episodes, transcripts)
    with memory_provenance(MemoryCause.DAY_EPISODES.value, UpdateStrategy.FULL.value):
        success, touched = await memory_service.add_day_memory(
            digest,
            day.local_date.isoformat(),
            day.user_id,
            source_date=datetime.combine(
                day.local_date, datetime.min.time(), tzinfo=ZoneInfo(day.timezone)
            ).isoformat(),
        )
    if not success:
        return "failed"

    async def record_paths(paths: list[str]) -> None:
        await TimelineEpisode.get_pymongo_collection().update_many(
            {"episode_id": {"$in": [episode.episode_id for episode in episodes]}},
            {"$set": {"memory_state": "written", "vault_paths": paths}},
        )

    if not touched:
        # The agent completed and chose to record nothing. Terminal, not a failure:
        # retrying only re-reaches the same judgement, and the day's legacy note
        # already carries the content it declined to duplicate.
        logger.info(
            "🗓️ Day %s for user %s needed no vault change (%d episode(s), "
            "%d episode note(s))",
            day.local_date,
            day.user_id,
            len(episodes),
            len(episode_notes),
        )
        await record_paths(episode_notes)
        return "no_changes"

    paths = list(dict.fromkeys([*episode_notes, *touched]))
    await record_paths(paths)
    logger.info(
        "🗓️ Recorded day %s for user %s: %d episode(s), %d note(s) touched "
        "(%d episode record note(s))",
        day.local_date,
        day.user_id,
        len(episodes),
        len(paths),
        len(episode_notes),
    )
    return "written"


async def write_day_memory(day: TimelineDay) -> str:
    """Claim one day and record it, settling ``memory_state`` however it ends.

    The claim is what makes this safe to call directly — a rebuild replaying a range of
    days and the cron scanning settled days can both reach the same day, and only one of
    them may spend an agent run on it. Returns the outcome, or ``"busy"`` when another
    holder has it.
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
            {"_id": claimed["_id"]},
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
        await collection.update_one(
            {"_id": claimed["_id"]},
            {
                "$set": {
                    "memory_state": outcome,
                    "memory_written_at": utcnow(),
                    "memory_error": None,
                }
            },
        )
    return outcome


async def process_episode_memory() -> dict[str, int]:
    """Record every settled day that has episodes but no vault entry yet."""

    totals = {
        "considered": 0,
        "written": 0,
        "no_changes": 0,
        "skipped": 0,
        "failed": 0,
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
