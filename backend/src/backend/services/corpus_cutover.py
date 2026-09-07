"""One-time planner for moving the development corpus to capture-owned audio.

This module is deliberately not a compatibility layer.  It accepts raw mappings from
the pre-cutover database and emits only current capture documents and audio claims.  No
runtime reader imports it, and no pre-cutover chunk model exists.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from bson import ObjectId

from backend.models.audio_capture import (
    AbsoluteWord,
    AudioRangeRef,
    DiarizationTurn,
    TranscriptUtterance,
)
from backend.services.audio_claims import map_presentation_interval
from backend.services.transcript_integrity import (
    TranscriptTimingError,
    validate_and_normalize_transcript_timing,
)

# Claims may elide a real gap by starting a new range, but they must never invent
# captured audio between adjacent chunks.  One millisecond covers timestamp rounding
# without swallowing the small real gaps observed in the development corpus.
RANGE_GAP_TOLERANCE_SECONDS = 0.001
_CUTOVER_NAMESPACE = uuid.UUID("a9fd33d9-3e51-45bf-a4d7-d7bb10dc8e89")

# Every source collection must be classified.  Unknown collections make the cutover
# stop instead of silently losing a newly-added durable record.
TRANSFORMED_COLLECTIONS = frozenset(
    {
        "audio_chunks",
        "audio_evidence_spans",
        "background_suppressions",
        "conversations",
        "device_input_items",
    }
)
COPIED_COLLECTIONS = frozenset(
    {
        "annotations",
        "api_keys",
        "audio_llm_response_cache",
        "background_clips",
        "background_cluster_reviews",
        "background_foreground_clips",
        "capture_sources",
        "chat_messages",
        "chat_sessions",
        "data_repair_audit",
        "enrollment_batches",
        "enrollment_reviews",
        "manual_memories",
        "media_role_overrides",
        "source_audit_reviews",
        "speaker_benchmark_runs",
        "speaker_label_reviews",
        "system_events",
        "transcription_response_cache",
        "users",
    }
)
REGENERATED_COLLECTIONS = frozenset(
    {
        # Rebuilt from the pre-cutover conversation-owned chunks. The source may
        # contain an empty collection created by current model initialization.
        "audio_capture_sessions",
        "background_cleanup_reports",
        "background_cluster_cache",
        "background_corpus_embeddings",
        "background_index_runs",
        "device_input_pairing_codes",
        "device_input_jobs",
        "drift_report_cache",
        "memory_audit",
        "speaker_corpus_embeddings",
        "speaker_corpus_matches",
        "speaker_discovery_runs",
        "speaker_evaluation_embeddings",
        "timeline_analysis_runs",
        "timeline_days",
        "timeline_episodes",
        "waveforms",
    }
)
HUMAN_REFERENCE_COLLECTIONS = frozenset(
    {
        "annotations",
        "background_clips",
        "background_foreground_clips",
        "background_cluster_reviews",
        "background_suppressions",
        "enrollment_reviews",
        "media_role_overrides",
        "source_audit_reviews",
        "speaker_label_reviews",
    }
)

FORBIDDEN_CHUNK_FIELDS = frozenset(
    {"conversation_id", "chunk_index", "start_time", "end_time"}
)
FORBIDDEN_CONVERSATION_FIELDS = frozenset({"always_persist", "source_session_id"})


def utc(value: datetime) -> datetime:
    """Mongo's naive datetimes are UTC, never node-local time."""

    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def classify_collections(
    names: Sequence[str],
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return copy/transform/regenerate/unknown sets for an auditable source schema."""

    source = {name for name in names if not name.startswith("system.")}
    known = COPIED_COLLECTIONS | TRANSFORMED_COLLECTIONS | REGENERATED_COLLECTIONS
    return (
        source & COPIED_COLLECTIONS,
        source & TRANSFORMED_COLLECTIONS,
        source & REGENERATED_COLLECTIONS,
        source - known,
    )


def _stable_id(kind: str, *parts: object) -> str:
    return str(
        uuid.uuid5(_CUTOVER_NAMESPACE, ":".join([kind, *(str(part) for part in parts)]))
    )


def _stable_object_id(kind: str, *parts: object) -> ObjectId:
    value = ":".join([kind, *(str(part) for part in parts)]).encode("utf-8")
    return ObjectId(hashlib.sha256(value).digest()[:12])


def _message_millis(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    head = value.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _relative_start(chunk: Mapping[str, Any], fallback: float) -> float:
    if chunk.get("start_time") is not None:
        return _number(chunk["start_time"])
    if chunk.get("chunk_index") is not None:
        return _number(chunk["chunk_index"]) * _number(chunk.get("duration"), 10.0)
    return fallback


def _group_key(chunk: Mapping[str, Any], position: int) -> tuple[int, float, str]:
    if chunk.get("chunk_index") is not None:
        return (0, _number(chunk["chunk_index"]), str(chunk.get("_id", position)))
    if chunk.get("start_time") is not None:
        return (1, _number(chunk["start_time"]), str(chunk.get("_id", position)))
    return (2, float(position), str(chunk.get("_id", position)))


def _transition_cost(previous: Mapping[str, Any], current: Mapping[str, Any]) -> float:
    """Cost of joining two source chunks; repeated WAL messages are very expensive."""

    if previous.get("source_stream") == current.get("source_stream"):
        previous_last = _message_millis(previous.get("source_last_message_id"))
        current_first = _message_millis(current.get("source_first_message_id"))
        if previous_last is not None and current_first is not None:
            delta = current_first - previous_last
            return abs(delta) + (1_000_000_000 if delta < -250 else 0)

    previous_time = previous.get("captured_at")
    current_time = current.get("captured_at")
    if isinstance(previous_time, datetime) and isinstance(current_time, datetime):
        expected = utc(previous_time) + timedelta(
            seconds=_number(previous.get("duration"))
        )
        delta = (utc(current_time) - expected).total_seconds() * 1000
        return abs(delta) + (1_000_000_000 if delta < -250 else 0)

    previous_end = _number(
        previous.get("end_time"),
        _relative_start(previous, 0.0) + _number(previous.get("duration")),
    )
    delta = (_relative_start(current, previous_end) - previous_end) * 1000
    return abs(delta) + (1_000_000_000 if delta < -250 else 0)


def select_operational_chunks(
    chunks: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Choose one coherent WAL path through duplicate operational chunk indexes.

    A duplicate index is not treated as two consecutive pieces of audio.  Dynamic
    programming chooses the candidate whose source-message/capture continuity best
    joins its neighbours.  Rejected bytes are reported and remain in the verified
    pre-cutover archive; they are never silently destroyed.
    """

    if not chunks:
        return [], []
    ordered = sorted(enumerate(chunks), key=lambda item: _group_key(item[1], item[0]))
    groups: list[list[Mapping[str, Any]]] = []
    group_identity: tuple[int, float] | None = None
    for position, chunk in ordered:
        key = _group_key(chunk, position)[:2]
        if key != group_identity:
            groups.append([])
            group_identity = key
        groups[-1].append(chunk)

    costs: list[float] = [0.0] * len(groups[0])
    parents: list[list[int]] = [[-1] * len(groups[0])]
    for group_index in range(1, len(groups)):
        previous_group = groups[group_index - 1]
        current_group = groups[group_index]
        next_costs: list[float] = []
        next_parents: list[int] = []
        for current in current_group:
            choices = [
                (
                    costs[previous_index] + _transition_cost(previous, current),
                    -_number(current.get("duration")),
                    str(current.get("_id", "")),
                    previous_index,
                )
                for previous_index, previous in enumerate(previous_group)
            ]
            winner = min(choices)
            next_costs.append(winner[0])
            next_parents.append(winner[3])
        costs = next_costs
        parents.append(next_parents)

    selected_indexes = [0] * len(groups)
    selected_indexes[-1] = min(
        range(len(groups[-1])),
        key=lambda index: (
            costs[index],
            -_number(groups[-1][index].get("duration")),
            str(groups[-1][index].get("_id", "")),
        ),
    )
    for group_index in range(len(groups) - 1, 0, -1):
        selected_indexes[group_index - 1] = parents[group_index][
            selected_indexes[group_index]
        ]

    selected = [
        group[index] for group, index in zip(groups, selected_indexes, strict=True)
    ]
    selected_ids = {str(chunk.get("_id")) for chunk in selected}
    quarantined = [
        chunk for chunk in chunks if str(chunk.get("_id")) not in selected_ids
    ]
    return selected, quarantined


def _capture_time_basis(conversation: Mapping[str, Any]) -> str:
    """State only the precision the old persistence path can actually prove."""

    if str(conversation.get("external_source_type") or "").lower() == "screenpipe":
        return "recorded"
    client_id = str(conversation.get("client_id") or "").lower()
    purpose = str(conversation.get("data_purpose") or "").lower()
    external_type = str(conversation.get("external_source_type") or "").lower()
    if (
        "upload" in client_id
        or "speaker-mining" in client_id
        or purpose == "annotation"
        or external_type == "annotation_dataset"
    ):
        return "unknown"
    return "received"


def _assign_capture_times(
    conversation: Mapping[str, Any], selected: Sequence[Mapping[str, Any]]
) -> tuple[list[datetime], str]:
    relative: list[float] = []
    cursor = 0.0
    for chunk in selected:
        start = _relative_start(chunk, cursor)
        relative.append(start)
        cursor = start + _number(chunk.get("duration"))

    anchors = [
        (relative[index], utc(chunk["captured_at"]))
        for index, chunk in enumerate(selected)
        if isinstance(chunk.get("captured_at"), datetime)
    ]
    basis = _capture_time_basis(conversation)
    if anchors:
        assigned = []
        for index, chunk in enumerate(selected):
            if isinstance(chunk.get("captured_at"), datetime):
                assigned.append(utc(chunk["captured_at"]))
                continue
            anchor_relative, anchor_time = min(
                anchors, key=lambda item: abs(item[0] - relative[index])
            )
            assigned.append(
                anchor_time + timedelta(seconds=relative[index] - anchor_relative)
            )
        return assigned, basis

    fallback = conversation.get("created_at")
    if not isinstance(fallback, datetime):
        created = [chunk.get("created_at") for chunk in selected]
        fallback = next((item for item in created if isinstance(item, datetime)), None)
    if not isinstance(fallback, datetime):
        raise ValueError(
            f"Conversation {conversation.get('conversation_id')} has no honest import anchor"
        )
    # In the live corpus every genuinely pre-WAL live recording already has at
    # least one persisted timestamp.  A wholly unanchored owner is an upload,
    # annotation, or other import surrogate even when its old client label is vague.
    basis = "unknown"
    base = utc(fallback)
    first_relative = min(relative) if relative else 0.0
    return [base + timedelta(seconds=item - first_relative) for item in relative], basis


@dataclass(frozen=True)
class CaptureAssignment:
    """Current capture identity assigned to one immutable source audio document."""

    capture_session_id: str
    capture_source_id: str
    user_id: str
    sequence: int
    captured_at: datetime
    time_basis: str


@dataclass(frozen=True)
class CaptureCorpusPlan:
    """Global technical sessions plus the assignment of every source audio blob."""

    capture_sessions: tuple[dict[str, Any], ...]
    assignments: Mapping[str, CaptureAssignment]


def _capture_sequence_key(
    chunk: Mapping[str, Any], assigned_at: datetime
) -> tuple[int, float, str]:
    first_message = _message_millis(chunk.get("source_first_message_id"))
    if first_message is not None:
        return (0, float(first_message), str(chunk["_id"]))
    return (1, assigned_at.timestamp(), str(chunk["_id"]))


def plan_capture_corpus(
    conversations: Mapping[str, Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
) -> CaptureCorpusPlan:
    """Recover technical sessions globally, independent of semantic grouping.

    A WAL stream is one technical capture attempt even when old split/trim operations
    spread its chunks across several Conversations.  Conversely, a merged Conversation
    may claim chunks from several streams.  Planning per Conversation would destroy
    both facts, so this function assigns every raw blob before semantic claims exist.
    """

    by_conversation: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in chunks:
        conversation_id = str(chunk.get("conversation_id") or "")
        if conversation_id not in conversations:
            raise ValueError(
                f"Audio chunk {chunk.get('_id')} has no source Conversation"
            )
        by_conversation.setdefault(conversation_id, []).append(chunk)

    assigned_times: dict[str, datetime] = {}
    time_bases: dict[str, str] = {}
    for conversation_id, items in by_conversation.items():
        ordered_items = [
            item
            for _, item in sorted(
                enumerate(items), key=lambda pair: _group_key(pair[1], pair[0])
            )
        ]
        times, basis = _assign_capture_times(
            conversations[conversation_id], ordered_items
        )
        for chunk, captured_at in zip(ordered_items, times, strict=True):
            chunk_id = str(chunk["_id"])
            assigned_times[chunk_id] = captured_at
            time_bases[chunk_id] = basis

    session_groups: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in chunks:
        source_stream = chunk.get("source_stream")
        conversation = conversations[str(chunk["conversation_id"])]
        external_source_id = conversation.get("external_source_id")
        external_source_type = str(conversation.get("external_source_type") or "")
        if isinstance(source_stream, str) and source_stream:
            key = f"stream:{conversation['user_id']}:{source_stream}"
        elif external_source_type == "screenpipe" and external_source_id:
            key = f"screenpipe:{conversation['user_id']}:{external_source_id}"
        elif external_source_id:
            key = (
                f"external:{conversation['user_id']}:{external_source_type}:"
                f"{external_source_id}"
            )
        else:
            key = f"unstreamed:{chunk['conversation_id']}"
        key = (
            f"{key}:basis:{time_bases[str(chunk['_id'])]}:"
            f"format:{int(chunk.get('sample_rate') or 16000)}:"
            f"{int(chunk.get('channels') or 1)}"
        )
        session_groups.setdefault(key, []).append(chunk)

    stream_group_counts: dict[str, int] = {}
    for items in session_groups.values():
        source_stream = items[0].get("source_stream")
        if isinstance(source_stream, str) and source_stream:
            stream_group_counts[source_stream] = (
                stream_group_counts.get(source_stream, 0) + 1
            )

    sessions: list[dict[str, Any]] = []
    assignments: dict[str, CaptureAssignment] = {}
    for session_key in sorted(session_groups):
        items = session_groups[session_key]
        source_conversations = {
            str(item["conversation_id"]): conversations[str(item["conversation_id"])]
            for item in items
        }
        users = {str(item["user_id"]) for item in source_conversations.values()}
        sources = {
            str(item.get("client_id") or f"import:{item['user_id']}")
            for item in source_conversations.values()
        }
        if len(users) != 1 or len(sources) != 1:
            raise ValueError(
                f"Technical session {session_key!r} crosses users or capture sources"
            )
        user_id = next(iter(users))
        capture_source_id = next(iter(sources))
        ordered = sorted(
            items,
            key=lambda item: _capture_sequence_key(
                item, assigned_times[str(item["_id"])]
            ),
        )
        session_id = _stable_id("capture-session", session_key)
        bases = {time_bases[str(item["_id"])] for item in ordered}
        session_basis = next(iter(bases)) if len(bases) == 1 else "unknown"
        for sequence, chunk in enumerate(ordered):
            chunk_id = str(chunk["_id"])
            assignments[chunk_id] = CaptureAssignment(
                capture_session_id=session_id,
                capture_source_id=capture_source_id,
                user_id=user_id,
                sequence=sequence,
                captured_at=assigned_times[chunk_id],
                time_basis=time_bases[chunk_id],
            )

        starts = [assigned_times[str(item["_id"])] for item in ordered]
        ends = [
            assigned_times[str(item["_id"])]
            + timedelta(seconds=_number(item.get("duration")))
            for item in ordered
        ]
        purposes = {
            str(item.get("data_purpose") or "normal_capture")
            for item in source_conversations.values()
        }
        first = ordered[0]
        source_stream = first.get("source_stream")
        session_source_stream = source_stream
        if (
            isinstance(source_stream, str)
            and stream_group_counts.get(source_stream, 0) > 1
        ):
            session_source_stream = (
                f"{source_stream}#cutover-{_stable_id('stream-part', session_key)[:8]}"
            )
        origins = {
            (
                "streaming"
                if item.get("source_stream")
                else (
                    "screenpipe"
                    if str(
                        source_conversations[str(item["conversation_id"])].get(
                            "external_source_type"
                        )
                        or ""
                    ).lower()
                    == "screenpipe"
                    else (
                        "upload"
                        if "upload"
                        in str(
                            source_conversations[str(item["conversation_id"])].get(
                                "client_id"
                            )
                            or ""
                        ).lower()
                        else "import"
                    )
                )
            )
            for item in ordered
        }
        if len(origins) != 1:
            raise ValueError(f"Technical session {session_key!r} crosses origins")
        sessions.append(
            {
                "_id": _stable_object_id("capture-session", session_key),
                "capture_session_id": session_id,
                "user_id": user_id,
                "capture_source_id": capture_source_id,
                "client_id": capture_source_id,
                "origin": next(iter(origins)),
                "time_basis": session_basis,
                "status": "complete",
                "source_stream": (
                    session_source_stream
                    if isinstance(session_source_stream, str)
                    else None
                ),
                "external_source_id": f"capture-cutover:{_stable_id('external-session', session_key)}",
                "content_sha256": None,
                "data_purpose": (
                    next(iter(purposes)) if len(purposes) == 1 else "normal_capture"
                ),
                "started_at": min(starts),
                "ended_at": max(ends),
                "sample_rate": int(first.get("sample_rate") or 16000),
                "channels": int(first.get("channels") or 1),
                "sample_width": 2,
                "created_at": min(
                    (
                        utc(item["created_at"])
                        for item in ordered
                        if isinstance(item.get("created_at"), datetime)
                    ),
                    default=min(starts),
                ),
                "failure": None,
            }
        )
    return CaptureCorpusPlan(tuple(sessions), assignments)


def convert_capture_chunk(
    source: Mapping[str, Any], assignment: CaptureAssignment
) -> dict[str, Any]:
    """Copy one encoded blob byte-for-byte while replacing only ownership coordinates."""

    return {
        "_id": source["_id"],
        "user_id": assignment.user_id,
        "capture_source_id": assignment.capture_source_id,
        "capture_session_id": assignment.capture_session_id,
        "sequence": assignment.sequence,
        "audio_data": source.get("audio_data"),
        "original_size": int(source.get("original_size") or 0),
        "compressed_size": int(source.get("compressed_size") or 0),
        "duration": _number(source.get("duration")),
        "captured_at": assignment.captured_at,
        "sample_rate": int(source.get("sample_rate") or 16000),
        "channels": int(source.get("channels") or 1),
        "source_stream": source.get("source_stream"),
        "source_first_message_id": source.get("source_first_message_id"),
        "source_last_message_id": source.get("source_last_message_id"),
        "source_message_ids": list(source.get("source_message_ids") or []),
        "vad": source.get("vad"),
        "created_at": source.get("created_at") or assignment.captured_at,
        # A deleted Conversation does not delete shared raw capture.  A chunk that was
        # itself disabled is different: copy its bytes, but never revive or claim it.
        "deleted": bool(source.get("deleted")),
        "deleted_at": source.get("deleted_at"),
        "deletion_reason": (
            source.get("deletion_reason")
            or ("pre_cutover_disabled" if source.get("deleted") else None)
        ),
    }


@dataclass(frozen=True)
class ConversationAudioPlan:
    """A semantic claim over globally-planned capture audio."""

    capture_sessions: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]
    audio_ranges: tuple[AudioRangeRef, ...]
    quarantined: tuple[dict[str, Any], ...]
    source_chunk_count: int
    selected_audio_sha256: str
    time_basis: str

    @property
    def duration_seconds(self) -> float:
        return sum(item.duration_seconds for item in self.audio_ranges)

    @property
    def claimed_chunk_count(self) -> int:
        return sum(len(item.chunk_ids) for item in self.audio_ranges)


def plan_conversation_audio(
    conversation: Mapping[str, Any],
    chunks: Sequence[Mapping[str, Any]],
    assignments: Mapping[str, CaptureAssignment] | None = None,
) -> ConversationAudioPlan:
    """Build one semantic claim; all source blobs remain in the capture layer."""

    if not chunks:
        return ConversationAudioPlan(
            (), (), (), (), 0, hashlib.sha256().hexdigest(), "unknown"
        )
    for field in ("conversation_id", "user_id"):
        if not conversation.get(field):
            raise ValueError(f"Source conversation is missing {field}")

    local_corpus: CaptureCorpusPlan | None = None
    if assignments is None:
        local_corpus = plan_capture_corpus(
            {str(conversation["conversation_id"]): conversation}, chunks
        )
        assignments = local_corpus.assignments

    enabled = [chunk for chunk in chunks if not chunk.get("deleted")]
    disabled = [chunk for chunk in chunks if chunk.get("deleted")]
    selected, rejected = select_operational_chunks(enabled)
    conversation_id = str(conversation["conversation_id"])
    converted = [
        convert_capture_chunk(chunk, assignments[str(chunk["_id"])]) for chunk in chunks
    ]

    ranges: list[AudioRangeRef] = []
    range_chunks: list[list[Mapping[str, Any]]] = []
    for chunk in selected:
        assignment = assignments[str(chunk["_id"])]
        if not range_chunks:
            range_chunks.append([chunk])
            continue
        previous_chunk = range_chunks[-1][-1]
        previous_assignment = assignments[str(previous_chunk["_id"])]
        expected = previous_assignment.captured_at + timedelta(
            seconds=_number(previous_chunk.get("duration"))
        )
        gap = (assignment.captured_at - expected).total_seconds()
        if (
            assignment.capture_source_id != previous_assignment.capture_source_id
            or assignment.time_basis != previous_assignment.time_basis
            or int(chunk.get("sample_rate") or 16000)
            != int(previous_chunk.get("sample_rate") or 16000)
            or int(chunk.get("channels") or 1)
            != int(previous_chunk.get("channels") or 1)
            or abs(gap) > RANGE_GAP_TOLERANCE_SECONDS
        ):
            range_chunks.append([chunk])
        else:
            range_chunks[-1].append(chunk)

    for items in range_chunks:
        first = items[0]
        last = items[-1]
        first_assignment = assignments[str(first["_id"])]
        last_assignment = assignments[str(last["_id"])]
        session_ids = list(
            dict.fromkeys(
                assignments[str(item["_id"])].capture_session_id for item in items
            )
        )
        chunk_ids = [str(item["_id"]) for item in items]
        started_at = first_assignment.captured_at
        ended_at = last_assignment.captured_at + timedelta(
            seconds=_number(last.get("duration"))
        )
        ranges.append(
            AudioRangeRef(
                range_id=_stable_id(
                    "audio-range",
                    first_assignment.capture_source_id,
                    first_assignment.time_basis,
                    ",".join(chunk_ids),
                    started_at.isoformat(),
                    ended_at.isoformat(),
                ),
                capture_source_id=first_assignment.capture_source_id,
                time_basis=first_assignment.time_basis,
                chunk_ids=chunk_ids,
                started_at=started_at,
                ended_at=ended_at,
                capture_session_ids=session_ids,
            )
        )

    audio_digest = hashlib.sha256()
    has_all_audio = True
    for chunk in converted:
        if chunk.get("audio_data") is None:
            has_all_audio = False
            break
        audio_digest.update(bytes(chunk["audio_data"]))

    quarantined = []
    for source, reason in [
        *((item, "overlapping_operational_index") for item in rejected),
        *((item, "pre_cutover_disabled") for item in disabled),
    ]:
        audio = source.get("audio_data")
        quarantined.append(
            {
                "_id": _stable_object_id("quarantine", source.get("_id")),
                "source_chunk_id": str(source.get("_id")),
                "source_conversation_id": conversation_id,
                "old_chunk_index": source.get("chunk_index"),
                "reason": reason,
                "audio_sha256": (
                    hashlib.sha256(bytes(audio)).hexdigest()
                    if audio is not None
                    else None
                ),
                "compressed_size": source.get("compressed_size"),
                "captured_at": source.get("captured_at"),
                "source_stream": source.get("source_stream"),
                "source_first_message_id": source.get("source_first_message_id"),
                "source_last_message_id": source.get("source_last_message_id"),
                "created_at": source.get("created_at")
                or source.get("captured_at")
                or datetime(1970, 1, 1, tzinfo=timezone.utc),
            }
        )

    return ConversationAudioPlan(
        capture_sessions=local_corpus.capture_sessions if local_corpus else (),
        chunks=tuple(converted),
        audio_ranges=tuple(ranges),
        quarantined=tuple(quarantined),
        source_chunk_count=len(chunks),
        selected_audio_sha256=audio_digest.hexdigest() if has_all_audio else "",
        time_basis=(
            next(iter({item.time_basis for item in ranges}))
            if len({item.time_basis for item in ranges}) == 1 and ranges
            else "mixed"
        ),
    )


def should_materialize_conversation(
    conversation: Mapping[str, Any], protected_ids: set[str]
) -> bool:
    """Keep real/user-touched semantics; leave technical owners in capture history."""

    conversation_id = str(conversation.get("conversation_id", ""))
    if conversation_id in protected_ids or bool(conversation.get("starred")):
        return True
    if conversation.get("deletion_reason") == "user_deleted":
        # A tombstone is user intent.  Retaining it prevents a later segmentation run
        # from silently recreating material the user explicitly removed.
        return True
    if bool(conversation.get("deleted")):
        return False
    if conversation.get("data_purpose") == "capture_evidence":
        return False
    if conversation_origin(conversation) == "deliberate":
        return True
    if "_has_meaningful_transcript" in conversation:
        return bool(conversation["_has_meaningful_transcript"])
    return any(
        str(version.get("transcript") or "").strip()
        or any(
            str(segment.get("text") or "").strip()
            for segment in version.get("segments") or []
        )
        for version in conversation.get("transcript_versions") or []
    )


def conversation_origin(conversation: Mapping[str, Any]) -> str:
    client = str(conversation.get("client_id") or "").lower()
    purpose = str(conversation.get("data_purpose") or "").lower()
    deliberate_markers = ("upload", "annotation", "speaker-mining", "webui-reco")
    if purpose == "annotation" or any(
        marker in client for marker in deliberate_markers
    ):
        return "deliberate"
    return "detected"


def build_conversation_document(
    source: Mapping[str, Any],
    plan: ConversationAudioPlan,
    *,
    active_revision_id: str | None,
    allowed_fields: set[str],
) -> dict[str, Any]:
    """Filter a source row to the current Conversation interface and attach claims."""

    document = {
        key: value
        for key, value in source.items()
        if key in allowed_fields and key not in FORBIDDEN_CONVERSATION_FIELDS
    }
    document["_id"] = source["_id"]
    document["audio_ranges"] = [
        item.model_dump(mode="python") for item in plan.audio_ranges
    ]
    if plan.audio_ranges:
        document["started_at"] = min(item.started_at for item in plan.audio_ranges)
        document["ended_at"] = max(item.ended_at for item in plan.audio_ranges)
    else:
        document["started_at"] = source.get("started_at") or source.get("created_at")
        document["ended_at"] = source.get("ended_at")
    origin = conversation_origin(source)
    document["origin"] = origin
    document["segmentation_key"] = (
        f"capture-cutover:v1:{source['conversation_id']}"
        if origin == "detected"
        else None
    )
    document["active_transcript_revision_id"] = active_revision_id
    document["audio_chunks_count"] = plan.claimed_chunk_count
    document["audio_total_duration"] = plan.duration_seconds
    if plan.chunks:
        original = sum(int(item.get("original_size") or 0) for item in plan.chunks)
        compressed = sum(int(item.get("compressed_size") or 0) for item in plan.chunks)
        document["audio_compression_ratio"] = (
            compressed / original if original else None
        )
    return document


def _content_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: (
            item.isoformat() if isinstance(item, datetime) else str(item)
        ),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _absolute_time(
    ranges: Sequence[AudioRangeRef],
    offset: float,
    *,
    prefer_previous_boundary: bool = False,
) -> datetime:
    """Map a validated presentation offset without silently repairing it.

    Presentation time has two valid wall-clock answers at a range boundary when
    the ranges have a capture gap.  Starts belong to the following range; ends
    belong to the preceding range.
    """

    total = sum(item.duration_seconds for item in ranges)
    remaining = float(offset)
    if remaining < 0 or remaining > total + 0.001:
        raise ValueError(f"timestamp {remaining} lies outside {total} seconds of audio")
    remaining = min(remaining, total)
    for index, audio_range in enumerate(ranges):
        duration = audio_range.duration_seconds
        at_boundary = remaining == duration and index < len(ranges) - 1
        if (
            remaining < duration
            or (at_boundary and prefer_previous_boundary)
            or index == len(ranges) - 1
        ):
            return utc(audio_range.started_at) + timedelta(
                seconds=min(remaining, duration)
            )
        remaining -= duration
    raise AssertionError("absolute timestamp mapping fell through")


@dataclass(frozen=True)
class _LegacyVersionEntry:
    conversation: Mapping[str, Any]
    audio_plan: ConversationAudioPlan
    version: Mapping[str, Any]
    ordinal: int
    version_id: str
    content_sha256: str

    @property
    def conversation_id(self) -> str:
        return str(self.conversation["conversation_id"])

    @property
    def identity(self) -> tuple[str, int]:
        return (self.conversation_id, self.ordinal)


@dataclass(frozen=True)
class ProcessingCorpusPlan:
    """Standalone evidence and revisions recovered from embedded legacy versions."""

    transcript_artifacts: tuple[dict[str, Any], ...]
    diarization_artifacts: tuple[dict[str, Any], ...]
    revisions_by_conversation: Mapping[str, tuple[dict[str, Any], ...]]
    active_revision_ids: Mapping[str, str | None]
    quarantined: tuple[dict[str, Any], ...]
    disposition_counts: Mapping[str, int]


def _version_created_at(entry: _LegacyVersionEntry) -> datetime:
    value = entry.version.get("created_at") or entry.conversation.get("created_at")
    return (
        utc(value)
        if isinstance(value, datetime)
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )


def _version_token(entry: _LegacyVersionEntry) -> tuple[object, ...]:
    return (
        entry.conversation_id,
        entry.ordinal,
        entry.version_id,
        entry.content_sha256,
    )


def _is_base_provider_version(entry: _LegacyVersionEntry) -> bool:
    metadata = entry.version.get("metadata") or {}
    return not metadata.get("derived") and not metadata.get("reprocessing_type")


def _is_pyannote_output(entry: _LegacyVersionEntry) -> bool:
    if str(entry.version.get("diarization_source") or "").lower() != "pyannote":
        return False
    metadata = entry.version.get("metadata") or {}
    reprocessing = metadata.get("reprocessing_type")
    return reprocessing == "speaker_diarization" or (
        not reprocessing and not metadata.get("derived")
    )


def _normalize_version_timing(
    entry: _LegacyVersionEntry,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    segments, words = validate_and_normalize_transcript_timing(
        list(entry.version.get("segments") or []),
        list(entry.version.get("words") or []),
        audio_duration=entry.audio_plan.duration_seconds,
    )
    for segment in segments:
        _, nested_words = validate_and_normalize_transcript_timing(
            [],
            list(segment.get("words") or []),
            audio_duration=entry.audio_plan.duration_seconds,
        )
        segment["words"] = nested_words
    source = {
        "segments": list(entry.version.get("segments") or []),
        "words": list(entry.version.get("words") or []),
    }
    normalized = {"segments": segments, "words": words}
    return segments, words, _content_digest(source) != _content_digest(normalized)


def _absolute_word(
    ranges: Sequence[AudioRangeRef], word: Mapping[str, Any]
) -> dict[str, Any]:
    start = float(word.get("start", 0.0))
    end = float(word.get("end", word.get("start", 0.0)) or 0.0)
    return AbsoluteWord(
        text=str(word.get("word", word.get("text", ""))),
        start_seconds=start,
        end_seconds=end,
        audio_spans=map_presentation_interval(ranges, start, end),
        confidence=word.get("confidence"),
        provider_speaker=(
            str(word["speaker"]) if word.get("speaker") is not None else None
        ),
    ).model_dump(mode="python")


def _transcript_artifact(
    entry: _LegacyVersionEntry,
    segments: Sequence[Mapping[str, Any]],
    words: Sequence[Mapping[str, Any]],
    *,
    timing_normalized: bool,
) -> dict[str, Any]:
    token = _version_token(entry)
    artifact_id = _stable_id("transcript-artifact", *token)
    ranges = entry.audio_plan.audio_ranges
    utterances = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", segment.get("start", 0.0)) or 0.0)
        utterances.append(
            TranscriptUtterance(
                text=str(segment.get("text") or ""),
                start_seconds=start,
                end_seconds=end,
                audio_spans=map_presentation_interval(ranges, start, end),
                words=[
                    _absolute_word(ranges, word) for word in segment.get("words") or []
                ],
                provider_speaker=(
                    str(segment["speaker"])
                    if segment.get("speaker") is not None
                    else None
                ),
                confidence=segment.get("confidence"),
            ).model_dump(mode="python")
        )
    return {
        "_id": _stable_object_id("transcript-artifact", *token),
        "artifact_id": artifact_id,
        "retry_key": f"capture-cutover:transcript:{artifact_id}",
        "user_id": str(entry.conversation["user_id"]),
        "capture_source_ids": list(
            dict.fromkeys(item.capture_source_id for item in ranges)
        ),
        "audio_ranges": [item.model_dump(mode="python") for item in ranges],
        "provider": str(entry.version.get("provider") or "pre-cutover"),
        "model": entry.version.get("model"),
        "status": "complete",
        "transcript": str(entry.version.get("transcript") or ""),
        "words": [_absolute_word(ranges, word) for word in words],
        "utterances": utterances,
        "raw_response": {
            "content_sha256": entry.content_sha256,
            "source_conversation_id": entry.conversation_id,
            "source_version_id": entry.version_id,
            "source_version_ordinal": entry.ordinal,
            "relative_words": list(entry.version.get("words") or []),
            "relative_segments": list(entry.version.get("segments") or []),
            "metadata": dict(entry.version.get("metadata") or {}),
            "timing_normalized_at_edge": timing_normalized,
            "cutover_preserved": True,
        },
        "created_at": _version_created_at(entry),
        "failure": None,
    }


def _diarization_artifact(
    entry: _LegacyVersionEntry,
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    token = _version_token(entry)
    artifact_id = _stable_id("diarization-artifact", *token)
    ranges = entry.audio_plan.audio_ranges
    payload = {
        "provider": "legacy-pyannote-derived",
        "segments": list(segments),
        "source_version_id": entry.version_id,
        "source_version_ordinal": entry.ordinal,
    }
    excluded_non_speech = 0
    excluded_nonpositive = 0
    turns: list[dict[str, Any]] = []
    for segment in segments:
        segment_type = str(segment.get("segment_type") or "speech")
        if segment_type != "speech" or str(segment.get("speaker") or "") == "system":
            excluded_non_speech += 1
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", segment.get("start", 0.0)) or 0.0)
        if end <= start:
            excluded_nonpositive += 1
            continue
        turns.append(
            DiarizationTurn(
                start_seconds=start,
                end_seconds=end,
                audio_spans=map_presentation_interval(ranges, start, end),
                speaker=str(segment.get("speaker") or "Unknown"),
                identified_as=segment.get("identified_as"),
                confidence=segment.get("confidence"),
                embedding=segment.get("embedding"),
            ).model_dump(mode="python")
        )
    return {
        "_id": _stable_object_id("diarization-artifact", *token),
        "artifact_id": artifact_id,
        "retry_key": f"capture-cutover:diarization:{artifact_id}",
        "user_id": str(entry.conversation["user_id"]),
        "capture_source_ids": list(
            dict.fromkeys(item.capture_source_id for item in ranges)
        ),
        "audio_ranges": [item.model_dump(mode="python") for item in ranges],
        "provider": "legacy-pyannote-derived",
        "model": None,
        "status": "complete",
        "turns": turns,
        "configuration": {
            "content_sha256": _content_digest(payload),
            "source_version_id": entry.version_id,
            "source_version_ordinal": entry.ordinal,
            "cutover_preserved": True,
            "excluded_non_speech_turns": excluded_non_speech,
            "excluded_nonpositive_turns": excluded_nonpositive,
        },
        "created_at": _version_created_at(entry),
        "failure": None,
    }


def _version_quarantine(
    entry: _LegacyVersionEntry, reason: str, details: Mapping[str, Any]
) -> dict[str, Any]:
    token = _version_token(entry)
    return {
        "_id": _stable_object_id("version-quarantine", reason, *token),
        "record_type": "legacy_transcript_version",
        "source_conversation_id": entry.conversation_id,
        "source_version_id": entry.version_id,
        "source_version_ordinal": entry.ordinal,
        "reason": reason,
        "content_sha256": entry.content_sha256,
        "details": dict(details),
        "created_at": _version_created_at(entry),
    }


@dataclass(frozen=True)
class VersionDisposition:
    """Small first-pass record; never retains transcript words or embeddings."""

    conversation_id: str
    ordinal: int
    version_id: str
    content_sha256: str
    source_version_id: str | None
    is_base_provider: bool
    is_pyannote: bool
    timing_normalized: bool
    transcript_artifact_id: str | None
    diarization_artifact_id: str | None
    transcript_error: Mapping[str, Any] | None
    diarization_error: Mapping[str, Any] | None

    @property
    def identity(self) -> tuple[str, int]:
        return (self.conversation_id, self.ordinal)


@dataclass(frozen=True)
class ProcessingLineageCatalog:
    dispositions: Mapping[tuple[str, int], VersionDisposition]
    local_versions: Mapping[tuple[str, str], tuple[tuple[str, int], ...]]
    global_versions: Mapping[str, tuple[tuple[str, int], ...]]
    disposition_counts: Mapping[str, int]

    def resolve(
        self, identity: tuple[str, int], *, artifact: str
    ) -> tuple[str | None, str | None]:
        """Walk source_version_id links to the nearest valid immutable artifact."""

        current = identity
        seen: set[tuple[str, int]] = set()
        while current not in seen:
            seen.add(current)
            disposition = self.dispositions[current]
            artifact_id = (
                disposition.transcript_artifact_id
                if artifact == "transcript"
                else disposition.diarization_artifact_id
            )
            if artifact_id:
                return artifact_id, None
            source_id = disposition.source_version_id
            if not source_id:
                return None, None
            local = self.local_versions.get(
                (disposition.conversation_id, source_id), ()
            )
            if local:
                # Preserve the old embedded model's first-match lookup behavior.
                current = local[0]
                continue
            global_matches = self.global_versions.get(source_id, ())
            if len(global_matches) == 1:
                current = global_matches[0]
                continue
            return None, "ambiguous" if global_matches else "missing"
        return None, "cycle"


@dataclass(frozen=True)
class ConversationProcessingDocuments:
    transcript_artifacts: tuple[dict[str, Any], ...]
    diarization_artifacts: tuple[dict[str, Any], ...]
    revisions: tuple[dict[str, Any], ...]
    active_revision_id: str | None
    quarantined: tuple[dict[str, Any], ...]
    disposition_counts: Mapping[str, int]


def _legacy_version_entry(
    conversation: Mapping[str, Any],
    audio_plan: ConversationAudioPlan,
    version: Mapping[str, Any],
    ordinal: int,
) -> _LegacyVersionEntry:
    version_id = str(version.get("version_id") or f"missing-{ordinal}")
    content = {
        "provider": version.get("provider"),
        "model": version.get("model"),
        "transcript": version.get("transcript") or "",
        "words": list(version.get("words") or []),
        "segments": list(version.get("segments") or []),
        "diarization_source": version.get("diarization_source"),
        "metadata": dict(version.get("metadata") or {}),
    }
    return _LegacyVersionEntry(
        conversation=conversation,
        audio_plan=audio_plan,
        version=version,
        ordinal=ordinal,
        version_id=version_id,
        content_sha256=_content_digest(content),
    )


def _timing_error(error: TranscriptTimingError) -> dict[str, Any]:
    return {"code": error.code, "message": str(error), **error.details}


def scan_processing_conversation(
    conversation: Mapping[str, Any], audio_plan: ConversationAudioPlan
) -> tuple[VersionDisposition, ...]:
    """First pass: validate/classify versions while retaining only tiny descriptors."""

    dispositions: list[VersionDisposition] = []
    for ordinal, version in enumerate(conversation.get("transcript_versions") or []):
        entry = _legacy_version_entry(conversation, audio_plan, version, ordinal)
        base = _is_base_provider_version(entry)
        pyannote = _is_pyannote_output(entry)
        normalized = False
        error_data: Mapping[str, Any] | None = None
        if base or pyannote:
            try:
                _, _, normalized = _normalize_version_timing(entry)
            except TranscriptTimingError as error:
                error_data = _timing_error(error)
        token = _version_token(entry)
        dispositions.append(
            VersionDisposition(
                conversation_id=entry.conversation_id,
                ordinal=entry.ordinal,
                version_id=entry.version_id,
                content_sha256=entry.content_sha256,
                source_version_id=(
                    str((version.get("metadata") or {})["source_version_id"])
                    if (version.get("metadata") or {}).get("source_version_id")
                    else None
                ),
                is_base_provider=base,
                is_pyannote=pyannote,
                timing_normalized=normalized,
                transcript_artifact_id=(
                    _stable_id("transcript-artifact", *token)
                    if base and error_data is None
                    else None
                ),
                diarization_artifact_id=(
                    _stable_id("diarization-artifact", *token)
                    if pyannote and error_data is None
                    else None
                ),
                transcript_error=error_data if base else None,
                diarization_error=error_data if pyannote else None,
            )
        )
    return tuple(dispositions)


def build_processing_lineage_catalog(
    dispositions: Sequence[VersionDisposition],
) -> ProcessingLineageCatalog:
    by_identity = {item.identity: item for item in dispositions}
    if len(by_identity) != len(dispositions):
        raise ValueError("processing version identities are not unique")
    local: dict[tuple[str, str], list[tuple[str, int]]] = {}
    global_versions: dict[str, list[tuple[str, int]]] = {}
    counts: dict[str, int] = {}

    def count(name: str, amount: int = 1) -> None:
        counts[name] = counts.get(name, 0) + amount

    for item in dispositions:
        local.setdefault((item.conversation_id, item.version_id), []).append(
            item.identity
        )
        global_versions.setdefault(item.version_id, []).append(item.identity)
        count("base_provider_versions" if item.is_base_provider else "derived_versions")
        if item.transcript_artifact_id:
            count("transcript_artifacts")
        if item.transcript_error:
            count("base_provider_timing_quarantined")
        if item.is_pyannote:
            count("pyannote_versions")
        if item.diarization_artifact_id:
            count("diarization_artifacts")
        if item.diarization_error:
            count("diarization_timing_quarantined")
        if item.timing_normalized:
            count("edge_timing_normalized")
    count(
        "duplicate_version_ids_within_conversation",
        sum(len(items) - 1 for items in local.values() if len(items) > 1),
    )
    count(
        "duplicate_version_ids_globally",
        sum(len(items) - 1 for items in global_versions.values() if len(items) > 1),
    )
    return ProcessingLineageCatalog(
        dispositions=by_identity,
        local_versions={key: tuple(value) for key, value in local.items()},
        global_versions={key: tuple(value) for key, value in global_versions.items()},
        disposition_counts=counts,
    )


def build_processing_conversation_documents(
    conversation: Mapping[str, Any],
    audio_plan: ConversationAudioPlan,
    catalog: ProcessingLineageCatalog,
) -> ConversationProcessingDocuments:
    """Second pass: materialize one bounded Conversation's artifacts and revisions."""

    transcript_artifacts: list[dict[str, Any]] = []
    diarization_artifacts: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def count(name: str, amount: int = 1) -> None:
        counts[name] = counts.get(name, 0) + amount

    entries = [
        _legacy_version_entry(conversation, audio_plan, version, ordinal)
        for ordinal, version in enumerate(conversation.get("transcript_versions") or [])
    ]
    for entry in entries:
        disposition = catalog.dispositions[entry.identity]
        if disposition.content_sha256 != entry.content_sha256:
            raise ValueError(
                f"Conversation {entry.conversation_id} version {entry.ordinal} changed between processing passes"
            )
        normalized_segments: list[dict[str, Any]] | None = None
        normalized_words: list[dict[str, Any]] | None = None
        if disposition.transcript_artifact_id or disposition.diarization_artifact_id:
            normalized_segments, normalized_words, normalized = (
                _normalize_version_timing(entry)
            )
            if normalized != disposition.timing_normalized:
                raise ValueError("processing timing changed between passes")
        if disposition.transcript_artifact_id:
            artifact = _transcript_artifact(
                entry,
                normalized_segments or [],
                normalized_words or [],
                timing_normalized=disposition.timing_normalized,
            )
            if artifact["artifact_id"] != disposition.transcript_artifact_id:
                raise AssertionError("transcript artifact identity drifted")
            transcript_artifacts.append(artifact)
        if disposition.diarization_artifact_id:
            artifact = _diarization_artifact(entry, normalized_segments or [])
            if artifact["artifact_id"] != disposition.diarization_artifact_id:
                raise AssertionError("diarization artifact identity drifted")
            diarization_artifacts.append(artifact)
        if disposition.transcript_error:
            error = disposition.transcript_error
            quarantined.append(
                _version_quarantine(
                    entry,
                    str(error["code"]),
                    {key: value for key, value in error.items() if key != "code"},
                )
            )
        elif disposition.diarization_error:
            error = disposition.diarization_error
            quarantined.append(
                _version_quarantine(
                    entry,
                    f"diarization_{error['code']}",
                    {key: value for key, value in error.items() if key != "code"},
                )
            )

        transcript_id, transcript_error = catalog.resolve(
            entry.identity, artifact="transcript"
        )
        diarization_id, diarization_error = catalog.resolve(
            entry.identity, artifact="diarization"
        )
        for kind, error in (
            ("transcript", transcript_error),
            ("diarization", diarization_error),
        ):
            if error:
                quarantined.append(
                    _version_quarantine(
                        entry,
                        f"unresolved_{kind}_lineage",
                        {"resolution": error},
                    )
                )
                count(f"unresolved_{kind}_lineage")

        source_metadata = dict(entry.version.get("metadata") or {})
        if "content_sha256" in source_metadata:
            source_metadata["cutover_source_content_sha256"] = source_metadata.pop(
                "content_sha256"
            )
        payload = {
            "conversation_id": entry.conversation_id,
            "transcript_artifact_ids": [transcript_id] if transcript_id else [],
            "diarization_artifact_ids": [diarization_id] if diarization_id else [],
            "transcript": entry.version.get("transcript") or "",
            "words": list(entry.version.get("words") or []),
            "segments": list(entry.version.get("segments") or []),
            "provider": entry.version.get("provider"),
            "model": entry.version.get("model"),
            "diarization_source": entry.version.get("diarization_source"),
            "metadata": {
                **source_metadata,
                "cutover_preserved": True,
                "source_version_id": entry.version_id,
                "source_version_ordinal": entry.ordinal,
                "source_content_sha256": entry.content_sha256,
            },
        }
        payload["metadata"]["content_sha256"] = _content_digest(payload)
        token = _version_token(entry)
        revision_id = _stable_id("conversation-revision", *token)
        revisions.append(
            {
                "_id": _stable_object_id("conversation-revision", *token),
                "revision_id": revision_id,
                "retry_key": f"capture-cutover:revision:{revision_id}",
                **payload,
                "created_at": _version_created_at(entry),
            }
        )
        count("conversation_revisions")

    active_id = conversation.get("active_transcript_version")
    active_revision_id = next(
        (
            revision["revision_id"]
            for entry, revision in zip(entries, revisions, strict=True)
            if active_id and entry.version_id == str(active_id)
        ),
        None,
    )
    if active_id and active_revision_id is None:
        count("missing_active_version")
    return ConversationProcessingDocuments(
        transcript_artifacts=tuple(transcript_artifacts),
        diarization_artifacts=tuple(diarization_artifacts),
        revisions=tuple(revisions),
        active_revision_id=active_revision_id,
        quarantined=tuple(quarantined),
        disposition_counts=counts,
    )


def build_processing_corpus(
    conversations: Sequence[Mapping[str, Any]],
    audio_plans: Mapping[str, ConversationAudioPlan],
) -> ProcessingCorpusPlan:
    """In-memory convenience wrapper; the live CLI uses the bounded two-pass API."""

    scanned = [
        disposition
        for conversation in conversations
        for disposition in scan_processing_conversation(
            conversation, audio_plans[str(conversation["conversation_id"])]
        )
    ]
    catalog = build_processing_lineage_catalog(scanned)
    transcript_artifacts: list[dict[str, Any]] = []
    diarization_artifacts: list[dict[str, Any]] = []
    revisions_by_conversation: dict[str, tuple[dict[str, Any], ...]] = {}
    active_revision_ids: dict[str, str | None] = {}
    quarantined: list[dict[str, Any]] = []
    counts = dict(catalog.disposition_counts)
    for conversation in conversations:
        conversation_id = str(conversation["conversation_id"])
        documents = build_processing_conversation_documents(
            conversation, audio_plans[conversation_id], catalog
        )
        transcript_artifacts.extend(documents.transcript_artifacts)
        diarization_artifacts.extend(documents.diarization_artifacts)
        revisions_by_conversation[conversation_id] = documents.revisions
        active_revision_ids[conversation_id] = documents.active_revision_id
        quarantined.extend(documents.quarantined)
        for name, amount in documents.disposition_counts.items():
            counts[name] = counts.get(name, 0) + amount
    return ProcessingCorpusPlan(
        transcript_artifacts=tuple(transcript_artifacts),
        diarization_artifacts=tuple(diarization_artifacts),
        revisions_by_conversation=revisions_by_conversation,
        active_revision_ids=active_revision_ids,
        quarantined=tuple(quarantined),
        disposition_counts=counts,
    )


def transform_audio_evidence_span(
    source: Mapping[str, Any],
    range_documents: Mapping[str, Sequence[Mapping[str, Any]]],
    materialized_ids: set[str],
    *,
    allowed_fields: set[str],
) -> dict[str, Any]:
    document = {key: value for key, value in source.items() if key in allowed_fields}
    document["_id"] = source["_id"]
    conversation_id = source.get("conversation_id")
    document["audio_ranges"] = list(range_documents.get(str(conversation_id), []))
    document["conversation_id"] = (
        str(conversation_id) if str(conversation_id) in materialized_ids else None
    )
    return document


def transform_device_input_item(
    source: Mapping[str, Any],
    materialized_ids: set[str],
    *,
    allowed_fields: set[str],
    recovered_media: bytes | None = None,
    recovered_media_filename: str | None = None,
    recovered_media_content_type: str | None = None,
) -> dict[str, Any]:
    document = {key: value for key, value in source.items() if key in allowed_fields}
    document["_id"] = source["_id"]
    conversation_id = source.get("conversation_id")
    document["conversation_id"] = (
        str(conversation_id) if str(conversation_id) in materialized_ids else None
    )
    document["related_conversation_ids"] = [
        str(item)
        for item in source.get("related_conversation_ids") or []
        if str(item) in materialized_ids
    ]
    metadata = dict(document.get("metadata") or {})
    old_promoted_path = source.get("promoted_path")
    old_vault_paths = list(source.get("vault_paths") or [])
    if old_promoted_path:
        metadata["cutover_source_promoted_path"] = str(old_promoted_path)
    if old_vault_paths:
        metadata["cutover_source_vault_paths"] = old_vault_paths
    if recovered_media is not None:
        digest = hashlib.sha256(recovered_media).hexdigest()
        expected = source.get("content_hash")
        if expected and str(expected).lower() != digest:
            raise ValueError(
                f"Recovered media hash {digest} does not match {expected} for {source['_id']}"
            )
        document["media_data"] = recovered_media
        document["content_hash"] = digest
        document["media_filename"] = recovered_media_filename
        document["media_content_type"] = recovered_media_content_type
        metadata["cutover_embedded_promoted_media_sha256"] = digest
    if old_promoted_path and document.get("media_data") is None:
        raise ValueError(
            f"Device input {source['_id']} would lose promoted media {old_promoted_path}"
        )
    document["metadata"] = metadata
    # The selected screenshot/media bytes and curation decision are evidence. Vault
    # paths are derived output and must be regenerated against the clean vault only
    # after any path-only media has been embedded and hash-verified above.
    document["promoted_path"] = None
    document["vault_paths"] = []
    if document.get("state") == "promoted":
        document["state"] = "linked"
    return document
