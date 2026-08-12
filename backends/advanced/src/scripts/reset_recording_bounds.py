"""Re-bound existing recordings the way the current pipeline would have bounded them.

Everything already stored was cut by the old rules: a blind 30-minute cap, and no
silence trim. ``captured_at`` now says exactly when each chunk's audio happened, so the
old cuts are no longer load-bearing — a capture window can be reassembled from the
chunks themselves and re-cut where the audio is actually quiet.

This does that in three steps per capture window, all of them existing operations:

    merge the pieces the cap severed  ->  split at speech-derived seams  ->  trim silence

Nothing is re-encoded and nothing is destroyed. Merge and split move chunk documents
and record lineage; the trim moves silence onto a soft-deleted remnant. Every chunk
keeps its ``captured_at``, so the result can be re-bounded again later.

    uv run python scripts/reset_recording_bounds.py                       # dry run
    uv run python scripts/reset_recording_bounds.py --scope screenpipe --apply

Scopes, narrowest first. ``fenced`` (the default) touches only ``capture_evidence``,
which is excluded from memory, so it has no vault consequence — but it also cannot
repair a window that a promoted recording sits inside, and those are the recordings a
user actually sees. ``screenpipe`` adds the promoted stretches and is what fixes the
Recordings page. ``all`` also re-bounds live device capture, whose memories must then
be rebuilt.

Re-running is a no-op: a window whose seams already match the plan reports no change,
so the tool can be run again after a trim shifts things without churning lineage.
"""

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Sequence, Tuple

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers.queue_controller import (
    start_post_conversation_jobs,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import AudioEvidenceSpan
from advanced_omi_backend.models.user import User
from advanced_omi_backend.models.waveform import WaveformData
from advanced_omi_backend.services.device_audio_ingest import plan_session_cuts
from advanced_omi_backend.utils.audio_trim import plan_silence_trim
from advanced_omi_backend.utils.vad_analysis import (
    frame_speech_intervals,
    merge_speech_regions,
)

logger = logging.getLogger("reset-bounds")

# Recordings whose audio ran together with no real gap belong to one capture window,
# whatever the old cap did to them. This matches the live ingest rule.
STREAM_GAP = timedelta(seconds=60)
# Resolution of the speech profile the cut planner reads.
BUCKET_SECONDS = 10.0

# Which recordings may be re-bound. Grouping always sees every live recording of a
# stream regardless of scope: a window whose middle recording is out of scope cannot be
# merged (nor should it be), and discovering that from the plan beats discovering it
# from a refused merge half way through an apply.
ALL_PURPOSES: List[str | None] = ["capture_evidence", "conversation", None]
SCOPES: dict[str, "Callable[[Recording], bool]"] = {
    # Continuous capture only. Excluded from memory, so no vault consequence.
    "fenced": lambda item: item.data_purpose == "capture_evidence",
    # All continuous capture, including the stretches promoted out of it. This is the
    # scope that fixes the Recordings page, because the 30-minute slabs a user actually
    # sees are the promoted ones.
    "screenpipe": lambda item: item.source_type == "screenpipe",
    # Also live device capture, whose memories must then be rebuilt.
    "all": lambda item: True,
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class Recording:
    """One existing conversation, positioned absolutely by its chunks."""

    conversation_id: str
    user_id: str
    client_id: str
    data_purpose: str | None
    source_type: str | None
    memory_exclusion_reason: str | None
    started_at: datetime
    duration: float
    chunk_count: int
    # Speech in conversation-relative seconds, from the chunks' stored VAD scores.
    speech: List[Tuple[float, float]]
    # False when some chunk was never analyzed, so ``speech`` understates the truth.
    analyzed: bool = True

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration)


@dataclass
class Window:
    """Contiguous capture, reassembled from recordings that adjoin in wall-clock."""

    sources: List[Recording] = field(default_factory=list)

    @property
    def started_at(self) -> datetime:
        return self.sources[0].started_at

    @property
    def duration(self) -> float:
        return sum(source.duration for source in self.sources)

    def speech(self) -> List[Tuple[float, float]]:
        """Speech intervals in window-relative seconds."""
        intervals: List[Tuple[float, float]] = []
        offset = 0.0
        for source in self.sources:
            intervals.extend(
                (offset + start, offset + end) for start, end in source.speech
            )
            offset += source.duration
        return intervals

    def promoted(self) -> List[Tuple[float, float]]:
        """Window-relative stretches already judged conversational. DISABLED.

        Carrying a promotion across a re-bound sounds right — the agent judged that
        stretch of *time* conversational, so the new bounds should inherit it. It is
        not, because the promotion is not stored against the time. It is stored as a
        flag on a container, and the container is a whole recording, most of which is
        not the conversation:

            [ ────────── 30-minute recording, flagged ────────── ]
                     ↑ the conversation is five minutes, in here

        Re-cut that and every child overlaps the flagged span, including the children
        holding none of the conversation. Those children are then sources for the next
        run, each flagged over its full length, and the flagged region grows again.
        Nothing in the loop ever re-asks whether someone was talking, so it can only
        widen: measured across two passes here, 17 → 23 recordings and 442 → 493
        minutes. Promoted also means memory-eligible, so the drift ends in the vault.

        This is the same defect as an episode citing a soft-deleted conversation, and
        as a transcript version stranded on a container that no longer owns the audio:
        an annotation keyed to a container is lost, or falsified, the moment the
        container is replaced — and re-bounding replaces containers by design.

        So a re-bound now leaves every child fenced, and visibility is re-derived from
        the episodes afterwards (``scripts/repromote_conversational_episodes.py``),
        where it is computed from the agent's bounds rather than inherited. Keeping
        this method as a no-op documents the decision at the place it was made.
        """
        return []

    def fence(self) -> Tuple[str | None, str | None]:
        """The purpose and reason a re-fenced child should carry."""
        for source in self.sources:
            if source.data_purpose == "capture_evidence":
                return source.data_purpose, source.memory_exclusion_reason
        return "capture_evidence", "continuous_screenpipe_capture"


@dataclass
class WindowPlan:
    window: Window
    cuts: List[float]
    # (start, end) of each target recording in window time, and what a trim saves.
    parts: List[Tuple[float, float]]
    trimmed_seconds: float
    severed: int

    @property
    def changes(self) -> bool:
        """Whether re-bounding this window would move anything.

        Comparing the planned seams against the ones already there is what makes a
        second run a no-op. Without it a window that was merged and split with nothing
        left to trim comes back looking changed forever — its pieces still adjoin, so
        they regroup into the same window and get re-cut at the same places, minting
        new conversation ids and a layer of lineage on every run.
        """
        if self.trimmed_seconds > 0.0:
            return True
        existing = []
        offset = 0.0
        for source in self.window.sources[:-1]:
            offset += source.duration
            existing.append(offset)
        if len(existing) != len(self.cuts):
            return True
        # One chunk of tolerance, because a cut is snapped to a chunk boundary when it
        # is applied (``_snap_split_points``). Comparing the planned second against the
        # snapped one instead makes every window disagree with itself forever.
        return any(abs(a - b) > BUCKET_SECONDS for a, b in zip(existing, self.cuts))


async def _speech_of(
    conversation_id: str, duration: float
) -> Tuple[List[Tuple[float, float]], bool]:
    """Speech intervals from stored per-chunk VAD scores, and whether they cover it all.

    The scores were written when the conversation was analyzed and travel with the
    chunk documents through every split and merge, so they are still addressed
    correctly after a re-bound.

    The coverage flag is not decoration. Audio nobody analyzed has no speech intervals,
    which is indistinguishable from silence unless the caller is told — and reading it
    as silence is the worst possible error here: the cut planner sees a uniformly quiet
    window and puts its cut at exactly the target, which is the blind 30-minute cut
    this tool exists to remove.
    """
    collection = AudioChunkDocument.get_pymongo_collection()
    cursor = collection.find(
        {"conversation_id": conversation_id, "deleted": {"$ne": True}},
        {"start_time": 1, "vad.scores": 1, "vad.frame_hop_ms": 1},
    ).sort("chunk_index", 1)
    raw: List[List[float]] = []
    analyzed = True
    async for chunk in cursor:
        vad = chunk.get("vad") or {}
        scores = vad.get("scores")
        if not scores:
            analyzed = False
            continue
        raw.extend(
            frame_speech_intervals(
                scores,
                float(vad["frame_hop_ms"]) / 1000.0,
                float(chunk["start_time"]),
            )
        )
    # No region cap. ``merge_speech_regions`` normally doubles its merge gap until the
    # list fits REGION_MAX_COUNT, which keeps a *playback* list compact but here would
    # swallow real silence into the speech around it and under-trim a long window.
    regions = merge_speech_regions(raw, duration, max_count=len(raw) + 1)
    return [(start, end) for start, end in regions], analyzed


async def load_recordings(purposes: Sequence[str | None]) -> List[Recording]:
    """Every live in-scope recording whose chunks carry an absolute anchor."""
    collection = AudioChunkDocument.get_pymongo_collection()
    recordings: List[Recording] = []
    unanchored = 0
    conversations = await Conversation.find(
        Conversation.deleted != True,  # noqa: E712 — Beanie needs ==
        {"data_purpose": {"$in": list(purposes)}},
    ).to_list()

    for conversation in conversations:
        bounds = await collection.aggregate(
            [
                {
                    "$match": {
                        "conversation_id": conversation.conversation_id,
                        "deleted": {"$ne": True},
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "first": {"$min": "$captured_at"},
                        "duration": {"$sum": "$duration"},
                        "count": {"$sum": 1},
                        "anchored": {
                            "$sum": {"$cond": [{"$ne": ["$captured_at", None]}, 1, 0]}
                        },
                    }
                },
            ]
        ).to_list(length=1)
        if not bounds or not bounds[0]["count"]:
            continue
        row = bounds[0]
        # A partial anchor cannot position the recording, and guessing the rest is the
        # failure mode the backfill was written to avoid. Leave it where it is.
        if row["anchored"] != row["count"] or row["first"] is None:
            unanchored += 1
            continue
        duration = float(row["duration"] or 0.0)
        speech, analyzed = await _speech_of(conversation.conversation_id, duration)
        recordings.append(
            Recording(
                conversation_id=conversation.conversation_id,
                user_id=conversation.user_id,
                client_id=conversation.client_id,
                data_purpose=conversation.data_purpose,
                source_type=conversation.external_source_type,
                memory_exclusion_reason=conversation.memory_exclusion_reason,
                started_at=_as_utc(row["first"]),
                duration=duration,
                chunk_count=int(row["count"]),
                speech=speech,
                analyzed=analyzed,
            )
        )

    if unanchored:
        logger.info("skipped %d recording(s) with no usable anchor", unanchored)
    return recordings


def group_windows(recordings: Sequence[Recording]) -> List[Window]:
    """Reassemble capture windows across the cuts the old cap made."""
    windows: List[Window] = []
    by_stream: dict[tuple[str, str], List[Recording]] = {}
    for recording in recordings:
        by_stream.setdefault((recording.user_id, recording.client_id), []).append(
            recording
        )

    for stream in by_stream.values():
        stream.sort(key=lambda item: item.started_at)
        current = Window(sources=[stream[0]])
        for recording in stream[1:]:
            if recording.started_at - current.sources[-1].ended_at > STREAM_GAP:
                windows.append(current)
                current = Window(sources=[recording])
            else:
                current.sources.append(recording)
        windows.append(current)
    return windows


def _fractions(
    speech: Sequence[Tuple[float, float]], duration: float
) -> List[float | None]:
    """Per-bucket speech share, the profile the cut planner reads."""
    buckets = max(1, int(duration // BUCKET_SECONDS))
    series: List[float | None] = [0.0] * buckets
    for start, end in speech:
        first = max(0, int(start // BUCKET_SECONDS))
        last = min(buckets - 1, int(end // BUCKET_SECONDS))
        for index in range(first, last + 1):
            low = index * BUCKET_SECONDS
            overlap = min(end, low + BUCKET_SECONDS) - max(start, low)
            if overlap > 0:
                series[index] = min(
                    1.0, (series[index] or 0.0) + overlap / BUCKET_SECONDS
                )
    return series


def _in_speech(time: float, speech: Sequence[Tuple[float, float]]) -> bool:
    return any(start <= time < end for start, end in speech)


def plan_window(
    window: Window, *, pad: float, min_run: float, min_saving: float
) -> WindowPlan:
    speech = window.speech()
    cuts = plan_session_cuts(_fractions(speech, window.duration), BUCKET_SECONDS)
    edges = [0.0, *cuts, window.duration]
    parts = list(zip(edges, edges[1:]))

    trimmed = 0.0
    for start, end in parts:
        chunks = [
            {
                "chunk_index": index,
                "start_time": offset,
                "end_time": min(offset + BUCKET_SECONDS, end - start),
                "duration": min(BUCKET_SECONDS, end - start - offset),
            }
            for index, offset in enumerate(_steps(0.0, end - start, BUCKET_SECONDS))
        ]
        local = [
            (max(0.0, s - start), min(end, e) - start)
            for s, e in speech
            if e > start and s < end
        ]
        plan = plan_silence_trim(
            chunks,
            local,
            pad_seconds=pad,
            min_run_seconds=min_run,
            min_saving_seconds=min_saving,
        )
        trimmed += plan.dropped_seconds

    return WindowPlan(
        window=window,
        cuts=cuts,
        parts=parts,
        trimmed_seconds=trimmed,
        severed=sum(1 for cut in cuts if _in_speech(cut, speech)),
    )


def _steps(start: float, stop: float, step: float) -> List[float]:
    values = []
    cursor = start
    while cursor < stop - 1e-6:
        values.append(cursor)
        cursor += step
    return values


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


async def apply_plan(plan: WindowPlan, user: User) -> str:
    """Merge, split and trim one window. Returns a one-line outcome."""
    # Imported here so a dry run never pulls in the controller's job machinery.
    from advanced_omi_backend.controllers import data_audit_controller as dac
    from advanced_omi_backend.workers.conversation_jobs import trim_silence

    target = plan.window.sources[0].conversation_id
    if len(plan.window.sources) > 1:
        result = await dac.merge_conversations(
            user, [source.conversation_id for source in plan.window.sources]
        )
        if not isinstance(result, dict):
            return f"merge refused: {getattr(result, 'body', b'')!r}"
        target = result["merged_conversation_id"]

    children = [(target, *plan.parts[0])] if len(plan.parts) == 1 else []
    if plan.cuts:
        result = await dac.split_conversation(user, target, plan.cuts)
        if not isinstance(result, dict):
            return f"split refused: {getattr(result, 'body', b'')!r}"
        children = [
            (child["conversation_id"], *part)
            for child, part in zip(result["children"], plan.parts)
        ]

    # Fence every child before trimming any of them. Merging through a visible
    # recording makes the whole merged span memory-eligible, which would hand hours of
    # ambient capture to the vault because it happened to adjoin a real call. Doing
    # this in its own pass means a failure later cannot leave half the window unfenced.
    #
    # Every child is fenced, including those covering audio that was visible before:
    # re-bounding does not decide visibility any more (see Window.promoted). Run
    # scripts/repromote_conversational_episodes.py afterwards to re-derive it.
    purpose, exclusion_reason = plan.window.fence()
    for child_id, _start, _end in children:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == child_id
        )
        if conversation is None:
            continue
        conversation.data_purpose = purpose
        conversation.memory_excluded = True
        conversation.memory_exclusion_reason = exclusion_reason
        await conversation.save()

    trimmed = 0.0
    for child_id, _start, _end in children:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == child_id
        )
        if conversation is None:
            continue
        speech, analyzed = await _speech_of(
            child_id, conversation.audio_total_duration or 0.0
        )
        if speech and analyzed:
            applied = await trim_silence(child_id, speech)
            if applied is not None:
                trimmed += applied.dropped_seconds

    await _finalize(children)
    return (
        f"{len(plan.window.sources)} -> {len(children)} recording(s), "
        f"trimmed {_hours(trimmed)}"
    )


async def _finalize(children: Sequence[tuple]) -> None:
    """Run the post-conversation chain once, on the final bounds.

    Merge and split each enqueue it themselves; during a re-set those fire on
    intermediate states — a five-hour merge that is about to be split and trimmed, and
    briefly memory-eligible. Suppressing them and running the chain here means titles
    describe what the recording ended up being, and no memory is ever extracted from a
    span that only existed inside this operation.
    """
    for child_id, _start, _end in children:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == child_id
        )
        if conversation is None or conversation.deleted:
            continue
        version = conversation.active_transcript
        if version is None:
            continue
        start_post_conversation_jobs(
            child_id,
            conversation.user_id,
            transcript_version_id=version.version_id,
            client_id=conversation.client_id,
            trigger=Conversation.ProcessingTrigger.REBOUND.value,
            skip_speaker_recognition=True,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=sorted(SCOPES), default="fenced")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--pad-seconds", type=float, default=5.0)
    parser.add_argument("--min-run-seconds", type=float, default=120.0)
    parser.add_argument("--min-saving-seconds", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=0, help="apply to N windows only")
    parser.add_argument(
        "--require-trim",
        action="store_true",
        help="apply only where silence actually moves (never re-cut seams alone)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    await init_beanie(
        database=client.chronicle,
        document_models=[
            Conversation,
            AudioChunkDocument,
            AudioEvidenceSpan,
            User,
            # The trim changes a chunk set in place, so it drops the caches derived
            # from it.
            WaveformData,
        ],
    )

    in_scope = SCOPES[args.scope]
    windows = []
    out_of_scope = []
    unanalyzed = []
    for window in group_windows(await load_recordings(ALL_PURPOSES)):
        if not all(source.analyzed for source in window.sources):
            # Cutting audio nobody has analyzed means cutting on no evidence: the
            # planner would see a uniformly quiet window and place its cut at exactly
            # the target, reinstating the blind 30-minute cut. Analyze it first.
            unanalyzed.append(window)
        elif all(in_scope(source) for source in window.sources):
            windows.append(window)
        else:
            out_of_scope.append(window)
    recordings = [source for window in windows for source in window.sources]
    plans = [
        plan_window(
            window,
            pad=args.pad_seconds,
            min_run=args.min_run_seconds,
            min_saving=args.min_saving_seconds,
        )
        for window in windows
    ]
    changed = [plan for plan in plans if plan.changes]

    before = sum(recording.duration for recording in recordings)
    after = before - sum(plan.trimmed_seconds for plan in plans)
    targets = sum(len(plan.parts) for plan in plans)
    cuts = sum(len(plan.cuts) for plan in plans)
    severed = sum(plan.severed for plan in plans)

    logger.info("scope %s", args.scope)
    # Windows with nothing in scope were never candidates. The ones worth naming are
    # those the scope splits: they hold in-scope audio that cannot be re-bound without
    # merging across a recording this scope may not touch.
    blocked = [
        window
        for window in out_of_scope
        if any(in_scope(source) for source in window.sources)
    ]
    if blocked:
        logger.info(
            "%d capture window(s) held back: an out-of-scope recording sits inside them",
            len(blocked),
        )
    logger.info(
        "%d recording(s) -> %d capture window(s) -> %d recording(s)",
        len(recordings),
        len(windows),
        targets,
    )
    logger.info("%d cut(s) placed, %d landing in speech", cuts, severed)
    logger.info(
        "audio %s -> %s (%s of silence moved to remnants)",
        _hours(before),
        _hours(after),
        _hours(before - after),
    )
    logger.info(
        "%d window(s) change, %d already correct",
        len(changed),
        len(plans) - len(changed),
    )
    if unanalyzed:
        logger.info(
            "%d window(s) held back as UNANALYZED (%s): no VAD scores, so nothing is "
            "known about where the speech is. Run audio analysis, then re-run.",
            len(unanalyzed),
            _hours(sum(window.duration for window in unanalyzed)),
        )
    # A window with no speech anywhere is left whole on purpose: trimming it would
    # empty it, and whether audio with no speech should exist at all is the speech
    # gate's decision, not this one's. Report it rather than quietly leaving it out.
    # This is only meaningful now that unanalyzed windows are excluded above — before
    # that, "no speech" and "never looked" were the same number.
    silent = [plan for plan in plans if not plan.window.speech()]
    if silent:
        logger.info(
            "%d window(s) carry no speech at all (%s) — left whole, not trimmed",
            len(silent),
            _hours(sum(plan.window.duration for plan in silent)),
        )

    visible = [
        source
        for plan in changed
        for source in plan.window.sources
        if source.data_purpose != "capture_evidence"
    ]
    if visible:
        logger.info(
            "%d currently-visible recording(s) (%s) are inside windows that change; "
            "they will be fenced. Re-derive visibility afterwards with "
            "scripts/repromote_conversational_episodes.py",
            len(visible),
            _hours(sum(source.duration for source in visible)),
        )

    longest = sorted(changed, key=lambda plan: -plan.window.duration)[:12]
    logger.info("\nlargest changes:")
    for plan in longest:
        speech = sum(end - start for start, end in plan.window.speech())
        logger.info(
            "  %s  %-6s  speech %3.0f%%  %2d src -> %2d rec  trim %s",
            plan.window.started_at.strftime("%Y-%m-%d %H:%M"),
            _hours(plan.window.duration),
            100 * speech / max(1.0, plan.window.duration),
            len(plan.window.sources),
            len(plan.parts),
            _hours(plan.trimmed_seconds),
        )

    if not args.apply:
        logger.info("\ndry run — nothing written")
        return

    user = await User.find_one(User.is_superuser == True)  # noqa: E712
    if user is None:
        raise SystemExit("no superuser to attribute the operations to")

    # Imported here so a dry run never pulls in the controller's job machinery.
    from advanced_omi_backend.controllers import data_audit_controller as dac

    # Merge and split each enqueue the post-conversation chain. _finalize runs it once
    # on the final bounds instead; see there for why the intermediate states must not.
    dac.start_post_conversation_jobs = lambda *args, **kwargs: {}

    # Newest first: a failure part-way leaves the oldest history untouched, and the
    # windows most likely to matter are verified first.
    queue = sorted(changed, key=lambda plan: plan.window.started_at, reverse=True)
    if args.require_trim:
        # A plan that moves no silence only re-cuts seams, and the 30-minute target is
        # a guess: on a dense continuous call it will happily slice one meeting into
        # three. That is worth nothing and can undo a boundary a human chose, so it is
        # not something to apply in bulk.
        held = [plan for plan in queue if plan.trimmed_seconds <= 0.0]
        queue = [plan for plan in queue if plan.trimmed_seconds > 0.0]
        for plan in held:
            logger.info(
                "  skipped (no silence to move) %s  %s speech %.0f%%  would be %d rec",
                plan.window.started_at.strftime("%Y-%m-%d %H:%M"),
                _hours(plan.window.duration),
                100
                * sum(end - start for start, end in plan.window.speech())
                / max(1.0, plan.window.duration),
                len(plan.parts),
            )
    if args.limit:
        queue = queue[: args.limit]
    logger.info("\napplying to %d window(s)", len(queue))
    for index, plan in enumerate(queue, start=1):
        outcome = await apply_plan(plan, user)
        logger.info(
            "  [%d/%d] %s  %s",
            index,
            len(queue),
            plan.window.started_at.strftime("%Y-%m-%d %H:%M"),
            outcome,
        )


if __name__ == "__main__":
    asyncio.run(main())
