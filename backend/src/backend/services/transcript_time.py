"""Resolve transcript timestamps to wall-clock time, and slice a transcript by it.

Every segment and word is stored as *seconds from the start of whatever container
held it*, so a timestamp only means anything relative to that container's current
bounds. Change the bounds — split, merge, silence trim — and every number is a lie
until something re-times it. That is why re-bounding has repeatedly cost annotations:
the text is keyed to the container, not to the audio.

``AudioChunkDocument.captured_at`` is immutable, so the wall-clock time is always
recoverable through a Conversation's range claims. Silence trimming clips those claims
and repacks the surviving presentation timeline, so the offset between relative and
absolute jumps at every seam. Measured on this deployment: 355 of 1093 conversations
(32%) have a moving anchor, 298 of them by more than a minute, the worst by 8.8 hours.
A single conversation-level anchor is therefore wrong for a third of the corpus, and
wrong silently — it yields a plausible time that is simply not when the words were
said. Resolution walks the chunk table instead.

With that, a transcript can be cut by an absolute range, which is what lets an
*episode* — the agent's semantic claim over a span of audio — carry the words actually
spoken inside it rather than the whole container that happens to hold them.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from backend.models.conversation import Conversation
from backend.services.audio_claims import resolve_conversation_audio

logger = logging.getLogger(__name__)

# A relative timestamp landing outside every chunk is resolved against the nearest
# one and flagged. It happens for real: a provider can emit segments that outrun the
# audio (30 of 641 conversations here, before silence trimming learned to re-time
# every version). Refusing to place them would drop real speech; placing them without
# saying so would invent wall-clock time. So it is bounded and reported.
MAX_EXTRAPOLATION = timedelta(minutes=5)


def as_utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; they are UTC, not node-local."""

    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


@dataclass(frozen=True)
class ChunkAnchor:
    """One chunk's mapping from container-relative seconds to wall-clock time."""

    start_time: float
    end_time: float
    captured_at: datetime

    def absolute(self, relative: float) -> datetime:
        return self.captured_at + timedelta(seconds=relative - self.start_time)


@dataclass
class AnchorMap:
    """Piecewise relative→absolute map for one conversation.

    Empty when no chunk carries ``captured_at``; ``resolve`` then returns ``None``
    rather than guessing, because an unanchored recording's wall-clock time is
    genuinely unknown and a plausible-but-wrong one corrupts everything downstream.
    """

    conversation_id: str
    anchors: list[ChunkAnchor] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.anchors.sort(key=lambda anchor: anchor.start_time)
        self._starts = [anchor.start_time for anchor in self.anchors]

    def __bool__(self) -> bool:
        return bool(self.anchors)

    @property
    def span(self) -> Optional[tuple[datetime, datetime]]:
        """Wall-clock bounds of the anchored audio, or ``None`` when unanchored.

        Not ``min``/``max`` of the anchor list: after a trim the chunks are not in
        wall-clock order relative to each other in any guaranteed way, so both ends
        are taken over resolved times.
        """

        if not self.anchors:
            return None
        edges = [anchor.captured_at for anchor in self.anchors]
        edges += [
            anchor.captured_at + timedelta(seconds=anchor.end_time - anchor.start_time)
            for anchor in self.anchors
        ]
        return min(edges), max(edges)

    def resolve(self, relative: float) -> Optional[datetime]:
        """Wall-clock time for a container-relative offset.

        Uses the chunk covering ``relative``; falls back to the nearest chunk when the
        offset sits in no chunk at all, and refuses beyond ``MAX_EXTRAPOLATION``.
        """

        if not self.anchors:
            return None
        index = bisect.bisect_right(self._starts, relative) - 1
        if index < 0:
            anchor = self.anchors[0]
        else:
            anchor = self.anchors[index]
            # Past this chunk's audio: the offset fell in a gap the trim left behind,
            # or beyond the end. Prefer the next chunk when one starts later.
            if relative > anchor.end_time and index + 1 < len(self.anchors):
                following = self.anchors[index + 1]
                if following.start_time - relative < relative - anchor.end_time:
                    anchor = following
        overflow = max(anchor.start_time - relative, relative - anchor.end_time, 0.0)
        if overflow > MAX_EXTRAPOLATION.total_seconds():
            logger.debug(
                "Offset %.1fs is %.1fs outside every chunk of %s; refusing to place it",
                relative,
                overflow,
                self.conversation_id,
            )
            return None
        return anchor.absolute(relative)


@dataclass(frozen=True)
class AbsoluteSegment:
    """A transcript segment placed on the wall clock."""

    conversation_id: str
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str
    identified_as: Optional[str] = None
    segment_type: str = "speech"

    @property
    def label(self) -> str:
        return self.identified_as or self.speaker or "Unknown"

    def overlaps(self, started_at: datetime, ended_at: datetime) -> bool:
        return self.started_at < ended_at and self.ended_at > started_at


async def load_anchor_map(conversation_id: str) -> AnchorMap:
    """Build the relative→absolute map from a Conversation's range claims."""
    claimed = await resolve_conversation_audio(conversation_id)
    anchors = [
        ChunkAnchor(
            start_time=item.conversation_start_seconds,
            end_time=item.conversation_start_seconds + item.duration_seconds,
            captured_at=as_utc(item.chunk.captured_at)
            + timedelta(seconds=item.clip_start_seconds),
        )
        for item in claimed
    ]
    return AnchorMap(conversation_id=conversation_id, anchors=anchors)


def place_segments(
    conversation: Conversation, anchors: AnchorMap
) -> list[AbsoluteSegment]:
    """Put a conversation's active-version segments on the wall clock.

    Segments that cannot be anchored are dropped rather than guessed at, and counted
    in the log: a transcript placed at the wrong time is worse than one absent, since
    it silently attributes speech to the wrong episode, the wrong day, and the wrong
    person's note.
    """

    if not anchors:
        return []
    placed: list[AbsoluteSegment] = []
    unplaced = 0
    for segment in conversation.segments or []:
        started_at = anchors.resolve(float(segment.start))
        ended_at = anchors.resolve(float(segment.end))
        if started_at is None or ended_at is None:
            unplaced += 1
            continue
        if ended_at < started_at:
            ended_at = started_at
        placed.append(
            AbsoluteSegment(
                conversation_id=conversation.conversation_id,
                started_at=started_at,
                ended_at=ended_at,
                text=(segment.text or "").strip(),
                speaker=segment.speaker or "",
                identified_as=segment.identified_as,
                segment_type=segment.segment_type or "speech",
            )
        )
    if unplaced:
        logger.info(
            "%s: %d of %d segments could not be anchored to wall-clock time",
            conversation.conversation_id,
            unplaced,
            len(conversation.segments or []),
        )
    placed.sort(key=lambda item: item.started_at)
    return placed


@dataclass
class RangeTranscript:
    """What was said inside an absolute time range, across any container."""

    started_at: datetime
    ended_at: datetime
    segments: list[AbsoluteSegment] = field(default_factory=list)
    # Containers consulted, and those that contributed nothing. Reported rather than
    # inferred: "no words in this range" and "this recording has no transcript at all"
    # are different facts and only one of them is a gap worth chasing.
    conversation_ids: list[str] = field(default_factory=list)
    unanchored_conversation_ids: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def render(self, timezone_name: Optional[str] = None) -> str:
        """Speaker-attributed text, one turn per line, timestamped in wall clock."""

        zone = timezone.utc
        if timezone_name:
            try:
                zone = ZoneInfo(timezone_name)
            except Exception:  # noqa: BLE001 - any bad zone falls back to UTC
                logger.warning("Unknown timezone %s; rendering in UTC", timezone_name)
        lines: list[str] = []
        for segment in self.segments:
            if not segment.text:
                continue
            stamp = segment.started_at.astimezone(zone).strftime("%H:%M:%S")
            lines.append(f"[{stamp}] {segment.label}: {segment.text}")
        return "\n".join(lines)


def segments_in_range(
    segments: Iterable[AbsoluteSegment], started_at: datetime, ended_at: datetime
) -> list[AbsoluteSegment]:
    start, end = as_utc(started_at), as_utc(ended_at)
    return [segment for segment in segments if segment.overlaps(start, end)]


async def conversations_overlapping(
    started_at: datetime, ended_at: datetime, *, user_id: Optional[str] = None
) -> list[str]:
    """Conversation IDs whose semantic claims overlap an absolute range.

    Raw capture is intentionally absent from this query: continuous audio that has not
    been materialized as speech must not appear as a user-visible Conversation.
    """

    start, end = as_utc(started_at), as_utc(ended_at)
    match: dict = {
        "deleted": {"$ne": True},
        "audio_ranges": {
            "$elemMatch": {
                "started_at": {"$lt": end},
                "ended_at": {"$gt": start},
                "time_basis": {"$ne": "unknown"},
            }
        },
    }
    if user_id is not None:
        match["user_id"] = user_id
    rows = (
        await Conversation.get_pymongo_collection()
        .find(match, {"conversation_id": 1})
        .to_list(length=None)
    )
    return sorted(str(row["conversation_id"]) for row in rows)


async def transcript_for_range(
    started_at: datetime,
    ended_at: datetime,
    *,
    conversation_ids: Optional[Sequence[str]] = None,
    user_id: Optional[str] = None,
) -> RangeTranscript:
    """The words spoken inside an absolute range, regardless of container.

    This is the read the whole re-anchoring exists to enable: an episode asks what was
    said between two wall-clock instants and gets exactly that, instead of the entire
    recording that happens to span it.
    """

    start, end = as_utc(started_at), as_utc(ended_at)
    candidates = (
        [str(item) for item in conversation_ids]
        if conversation_ids is not None
        else await conversations_overlapping(start, end, user_id=user_id)
    )
    result = RangeTranscript(
        started_at=start, ended_at=end, conversation_ids=candidates
    )
    if not candidates:
        return result
    conversations = await Conversation.find(
        {"conversation_id": {"$in": sorted(set(candidates))}}
    ).to_list()
    for conversation in conversations:
        anchors = await load_anchor_map(conversation.conversation_id)
        if not anchors:
            result.unanchored_conversation_ids.append(conversation.conversation_id)
            continue
        result.segments.extend(
            segments_in_range(place_segments(conversation, anchors), start, end)
        )
    result.segments.sort(key=lambda item: item.started_at)
    return result
