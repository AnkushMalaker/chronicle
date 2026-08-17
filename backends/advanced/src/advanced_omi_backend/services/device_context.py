"""Lifecycle for conversation screen context: request, filter, and expire.

Screen context is the one capture stream Chronicle pulls rather than receives.
A completed conversation opens a bounded OCR job; the collector answers with
every frame in that window, so the reduction the observation state machine does
on-device has to happen here instead — first when the job completes
(``select_context_items``), then when the owning conversation goes away
(``purge_screen_context``).
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

from advanced_omi_backend.config import get_screen_context_settings
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    utcnow,
)

logger = logging.getLogger(__name__)

_IDENTITY_FIELDS = ("app_name", "window_name", "browser_url")
# Delete in batches so one sweep never builds an unbounded `$in` query.
_PURGE_BATCH = 500
# Ported from ScreenPipe's own text dedup, crates/screenpipe-db/src/text_similarity.rs
# (`is_similar_words`) and db/mod.rs `DEDUP_SIMILARITY_THRESHOLD`. It ships there for
# cross-device audio-transcription dedup; the comparison is the same shape, so we use
# their calibration rather than inventing one. Their containment test is deliberately
# NOT ported — see `_is_similar`.
_MIN_DEDUP_WORDS = 4
# ScreenPipe reports how it got a frame's text. `accessibility`/`hybrid` come from the
# AT-SPI tree and are dense and stable; `ocr` is the visual fallback used when a window
# exposes no tree at all — every fullscreen frame on this KDE Wayland box. Word overlap
# is evidence for the former and noise for the latter.
_STRUCTURED_TEXT_SOURCES = {"accessibility", "hybrid"}
# ScreenPipe's record of why it grabbed a frame. These two are explicit capture events
# rather than incidental samples, so they are never collapsed into a neighbour.
_ALWAYS_KEEP_TRIGGERS = {"manual", "window_focus"}


class ContextItem(Protocol):
    """The shape ``select_context_items`` needs from a job-completion item."""

    captured_at: datetime
    metadata: dict[str, Any]


class ConversationWindow(Protocol):
    """The four facts ``request_conversation_context_jobs`` needs from a conversation.

    It only derives a bounded time window and an owner, so a caller sweeping many
    conversations can project these fields instead of loading whole documents —
    a conversation carries its transcripts, and 69 of them are 63 MB here.
    """

    user_id: str
    conversation_id: str
    created_at: datetime
    audio_total_duration: float | None


async def request_conversation_context_jobs(
    conversation: ConversationWindow,
) -> list[str]:
    margin = timedelta(minutes=5)
    start_at = conversation.created_at - margin
    end_at = (
        conversation.created_at
        + timedelta(seconds=conversation.audio_total_duration or 0)
        + margin
    )
    sources = await CaptureSource.find(
        CaptureSource.user_id == conversation.user_id,
        CaptureSource.provider == "screenpipe",
    ).to_list()
    # Immich discovery is asynchronous; link metadata-only candidates already
    # known for the same bounded interval without downloading their pixels.
    immich_items = await DeviceInputItem.find(
        DeviceInputItem.user_id == conversation.user_id,
        DeviceInputItem.kind == "immich_memory",
        DeviceInputItem.captured_at >= start_at,
        DeviceInputItem.captured_at <= end_at,
        DeviceInputItem.conversation_id == None,  # noqa: E711
    ).to_list()
    for item in immich_items:
        item.conversation_id = conversation.conversation_id
        if item.state != "promoted":
            item.state = "linked"
        await item.save()
    jobs: list[str] = []
    for source in sources:
        existing = await DeviceInputJob.find_one(
            {
                "source_id": source.source_id,
                "purpose": "conversation_enrichment",
                "payload.conversation_id": conversation.conversation_id,
                "status": {"$in": ["pending", "claimed", "complete"]},
            }
        )
        if existing:
            jobs.append(str(existing.id))
            continue
        job = DeviceInputJob(
            user_id=conversation.user_id,
            source_id=source.source_id,
            kind="screen_context",
            start_at=start_at,
            end_at=end_at,
            purpose="conversation_enrichment",
            payload={"conversation_id": conversation.conversation_id},
        )
        await job.insert()
        jobs.append(str(job.id))
    return jobs


def _normalized_text(item: ContextItem) -> str:
    return " ".join(str(item.metadata.get("text") or "").split())


def _identity(item: ContextItem) -> tuple[str, ...]:
    return tuple(
        str(item.metadata.get(field) or "").strip() for field in _IDENTITY_FIELDS
    )


def _normalize_words(text: str) -> list[str]:
    """ScreenPipe's normalization: lowercase, drop punctuation, split on whitespace."""
    return "".join(
        character
        for character in text.lower()
        if character.isalnum() or character.isspace()
    ).split()


def _word_jaccard(words1: list[str], words2: list[str]) -> float:
    set1, set2 = set(words1), set(words2)
    union = set1 | set2
    if not union:
        return 1.0
    return len(set1 & set2) / len(union)


def _is_similar(words1: list[str], words2: list[str], threshold: float) -> bool:
    """Port of ScreenPipe's ``is_similar_words`` (screenpipe-db/src/text_similarity.rs).

    Their containment test is intentionally left out. It exists because for a
    duplicated audio transcription either copy is equally good, so folding a
    short segment into a longer one loses nothing. A screen that grew — a page
    scrolled, a thread gained a message — contains its own earlier text, so
    containment would fire and discard the *more* complete frame.
    """
    if len(words1) < _MIN_DEDUP_WORDS and len(words2) < _MIN_DEDUP_WORDS:
        return words1 == words2
    return _word_jaccard(words1, words2) >= threshold


def _text_source(item: ContextItem) -> str:
    return str(item.metadata.get("text_source") or "").strip().lower()


def _capture_trigger(item: ContextItem) -> str:
    return str(item.metadata.get("capture_trigger") or "").strip().lower()


def _is_comparable(item: ContextItem, identity: tuple[str, ...]) -> bool:
    """Whether word overlap is evidence for this frame, per ScreenPipe's own label.

    Frames whose text came from the accessibility tree are dense and stable, and
    are the same set ScreenPipe's own SimHash cache dedups. OCR-only frames are
    the visual fallback for windows exposing no tree — fullscreen sessions above
    all — where the text is a few HUD fragments that read alike whether or not
    the moment is the same. Inferring from that vocabulary is what
    `screen-event-extraction-lab` measured at 0/15 human-reviewed.

    Collectors predating `text_source` forwarding fall back to window identity,
    which on this box is present for ~100% of accessibility frames and absent for
    ~76% of OCR-only ones.
    """
    source = _text_source(item)
    if source:
        return source in _STRUCTURED_TEXT_SOURCES
    return any(identity)


def select_context_items(
    items: Iterable[ContextItem],
    *,
    max_bytes: int,
    similarity_threshold: float,
) -> tuple[list[ContextItem], dict[str, Any]]:
    """Collapse a window of per-frame OCR down to the frames that say something new.

    Two filters, deliberately unequal in confidence:

    * Exact duplicates go everywhere. Identical text in an identical window is
      the same screen by construction, no judgement involved.
    * Near-duplicates go only where token overlap is evidence — a text-dense
      frame in a known window, compared against the last *kept* frame so slow
      drift still accumulates into a new sample.

    Sparse and contextless frames are kept whole. They are 45% of the frames but
    4% of the bytes here, so collapsing them buys almost no storage while
    discarding the frames most likely to be the only record of a fullscreen
    session. The bound is therefore a byte budget, not a frame count.
    """
    ordered = sorted(items, key=lambda row: row.captured_at)
    kept: list[ContextItem] = []
    report = {"empty": 0, "duplicate": 0, "over_budget": 0, "kept_bytes": 0}
    seen: set[str] = set()
    previous_identity: tuple[str, ...] | None = None
    previous_words: list[str] = []
    for item in ordered:
        text = _normalized_text(item)
        identity = _identity(item)
        if not text and not any(identity):
            report["empty"] += 1
            continue
        fingerprint = hashlib.sha256(
            "\x1f".join([*identity, text]).encode("utf-8")
        ).hexdigest()
        if fingerprint in seen:
            report["duplicate"] += 1
            continue
        words = _normalize_words(text)
        comparable = _is_comparable(item, identity)
        if (
            comparable
            and _capture_trigger(item) not in _ALWAYS_KEEP_TRIGGERS
            and identity == previous_identity
            and _is_similar(previous_words, words, similarity_threshold)
        ):
            report["duplicate"] += 1
            continue
        size = len(text.encode("utf-8"))
        if report["kept_bytes"] + size > max_bytes:
            report["over_budget"] += 1
            continue
        seen.add(fingerprint)
        if comparable:
            previous_identity = identity
            previous_words = words
        report["kept_bytes"] += size
        kept.append(item)
    return kept, report


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _expired_conversation_ids(
    conversation_ids: list[str], cutoff: datetime
) -> list[str]:
    """Referenced conversations that are gone, or soft-deleted past the cutoff.

    A soft delete is restorable, so its context survives the retention window
    with it; only ``deleted_at`` older than the cutoff makes the context
    unreachable for good. A conversation missing entirely was hard-deleted, and
    that never comes back.
    """
    expired: list[str] = []
    for index in range(0, len(conversation_ids), _PURGE_BATCH):
        batch = conversation_ids[index : index + _PURGE_BATCH]
        # Three small fields decide this, but hydrating the documents would pull
        # every transcript of up to 500 conversations through the event loop.
        rows = await (
            Conversation.get_pymongo_collection()
            .find(
                {"conversation_id": {"$in": batch}},
                {"conversation_id": 1, "deleted": 1, "deleted_at": 1},
            )
            .to_list(length=None)
        )
        alive = {row["conversation_id"] for row in rows}
        expired.extend(item for item in batch if item not in alive)
        for row in rows:
            deleted_at = row.get("deleted_at")
            if not row.get("deleted") or deleted_at is None:
                continue
            if _as_utc(deleted_at) <= cutoff:
                expired.append(row["conversation_id"])
    return expired


async def purge_screen_context() -> dict[str, Any]:
    """Expire screen context whose conversation is gone, or that lost its owner.

    Screen context is only ever reachable through its conversation, so it has no
    meaning once that conversation is hard-deleted or has been soft-deleted past
    the retention window. Items unlinked by ``clear_conversation_context`` expire
    on the same window. Promoted items back durable vault media and are never
    swept.
    """
    settings = get_screen_context_settings()
    cutoff = utcnow() - timedelta(days=settings["retention_days"])
    collection = DeviceInputItem.get_pymongo_collection()
    sweepable = {"kind": "screen_context", "state": {"$ne": "promoted"}}

    referenced = await collection.distinct(
        "conversation_id", {**sweepable, "conversation_id": {"$ne": None}}
    )
    expired = await _expired_conversation_ids([str(row) for row in referenced], cutoff)
    orphaned_removed = 0
    conversation_removed = 0

    for index in range(0, len(expired), _PURGE_BATCH):
        result = await collection.delete_many(
            {
                **sweepable,
                "conversation_id": {"$in": expired[index : index + _PURGE_BATCH]},
            }
        )
        conversation_removed += result.deleted_count

    result = await collection.delete_many(
        {**sweepable, "conversation_id": None, "created_at": {"$lte": cutoff}}
    )
    orphaned_removed = result.deleted_count

    summary = {
        "referenced_conversations": len(referenced),
        "expired_conversations": len(expired),
        "removed_with_conversation": conversation_removed,
        "removed_orphaned": orphaned_removed,
        "retention_days": settings["retention_days"],
    }
    if conversation_removed or orphaned_removed:
        logger.info("Screen-context retention sweep: %s", summary)
    return summary
