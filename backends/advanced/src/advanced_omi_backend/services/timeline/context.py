"""Compact a day's multimodal evidence into non-duplicated segmentation blocks.

Coverage windows overlap deliberately, which is useful for validation but wasteful as
an agent input: a long transcript or screen session is repeated in every window it
touches. Context blocks assign each evidence item exactly once, retain its authoritative
ID and bounds, and bound verbose OCR/accessibility text before a local model condenses
dense blocks. The final segmentation model therefore sees temporal structure rather
than megabytes of repeated JSON.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .contracts import TimelineEvidenceItem, TimelineEvidenceManifest

CONTEXT_VERSION = "hierarchical-v3-gap-safe"
DEFAULT_BLOCK_MAX_CHARS = 80000
DEFAULT_BLOCK_MAX_ITEMS = 160
DEFAULT_DENSE_MIN_CHARS = 50000
DEFAULT_DENSE_MIN_ITEMS = 80
_TRANSCRIPT_EXCERPT_CHARS = 5000
_OTHER_EXCERPT_CHARS = 800
_POINT_WIDTH = timedelta(seconds=1)
_MAX_BUNDLED_GAP = timedelta(minutes=5)
_MAX_BUNDLED_SPAN = timedelta(hours=1)
_SCREEN_BUCKET_SECONDS = 5 * 60
_MAX_FALLBACK_EVENTS = 8
_MAX_FINAL_EVENTS_PER_BLOCK = 16
_FALLBACK_SNIPPET_CHARS = 180
_SEMANTIC_METADATA_KEYS = frozenset(
    {
        "app_name",
        "browser_url",
        "coalesced_count",
        "conversation_id",
        "direction",
        "meeting_id",
        "missing_seconds",
        "observation_scope",
        "sample_count",
        "source_kind",
        "speakers",
        "transcript_block_count",
        "transcript_block_index",
        "window_name",
    }
)


class TimelineContextEvent(BaseModel):
    """One local-condenser observation, still grounded in original evidence IDs."""

    started_at: datetime
    ended_at: datetime
    summary: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(min_length=1)
    modalities: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    image_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def positive_duration(self) -> "TimelineContextEvent":
        if self.ended_at < self.started_at:
            raise ValueError("context event ends before it starts")
        if self.ended_at == self.started_at:
            self.ended_at += _POINT_WIDTH
        return self


class TimelineContextSummary(BaseModel):
    events: list[TimelineContextEvent]
    unresolved_evidence_ids: list[str] = Field(default_factory=list)


def compact_evidence(item: TimelineEvidenceItem) -> dict[str, Any]:
    """Agent-facing evidence with exact identity/ranges and bounded semantic text."""

    excerpt_limit = (
        _TRANSCRIPT_EXCERPT_CHARS if item.kind == "transcript" else _OTHER_EXCERPT_CHARS
    )
    metadata = {
        key: value
        for key, value in item.metadata.items()
        if key in _SEMANTIC_METADATA_KEYS and value not in (None, "", [], {})
    }
    excerpt = item.excerpt or ""
    if item.kind == "observation":
        for key in ("app_name", "window_name", "browser_url"):
            value = str(item.metadata.get(key) or "")
            if value:
                excerpt = excerpt.replace(value, "")
        excerpt = excerpt.strip(" ·\n\t")
    return {
        "evidence_ids": [item.evidence_id],
        "kind": item.kind,
        "source_id": item.source_id,
        "started_at": item.started_at.isoformat(),
        "ended_at": (item.ended_at or item.started_at).isoformat(),
        "role": item.role,
        "excerpt": excerpt[:excerpt_limit] or None,
        "metadata": metadata,
        "image_evidence_ids": [item.evidence_id] if item.image_filename else [],
    }


def _screen_bucket(value: datetime) -> int:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int(aware.timestamp()) // _SCREEN_BUCKET_SECONDS


def compact_evidence_groups(
    manifest: TimelineEvidenceManifest,
) -> list[dict[str, Any]]:
    """Collapse screen noise into five-minute transition bundles.

    This is deterministic transport compaction, not semantic segmentation. It says
    which screens appeared within a bounded bucket and preserves every original ID;
    the local condenser/final agent still decide which changes form an activity.
    """

    groups: list[dict[str, Any]] = []
    screen: dict[tuple[str, int], dict[str, Any]] = {}
    for item in sorted(
        manifest.evidence, key=lambda value: (value.started_at, value.evidence_id)
    ):
        entry = compact_evidence(item)
        if item.kind != "observation":
            groups.append(entry)
            continue
        key = (item.source_id or "", _screen_bucket(item.started_at))
        group = screen.get(key)
        transition = " · ".join(
            part
            for part in (
                item.started_at.isoformat(),
                str(item.metadata.get("app_name") or ""),
                str(item.metadata.get("window_name") or ""),
                entry.get("excerpt") or "",
            )
            if part
        )
        if group is None:
            group = {
                "evidence_ids": [],
                "kind": "observation",
                "source_id": item.source_id,
                "started_at": item.started_at.isoformat(),
                "ended_at": (item.ended_at or item.started_at).isoformat(),
                "role": item.role,
                "excerpt": "",
                "metadata": {"screen_bucket_minutes": 5},
                "image_evidence_ids": [],
                "_transitions": [],
            }
            screen[key] = group
            groups.append(group)
        group["evidence_ids"].append(item.evidence_id)
        group["started_at"] = min(group["started_at"], item.started_at.isoformat())
        group["ended_at"] = max(
            group["ended_at"], (item.ended_at or item.started_at).isoformat()
        )
        if transition and transition not in group["_transitions"]:
            group["_transitions"].append(transition)
        if item.image_filename:
            group["image_evidence_ids"].append(item.evidence_id)

    for group in screen.values():
        transitions = group.pop("_transitions")
        if len(transitions) > 10:
            transitions = [*transitions[:5], "…", *transitions[-5:]]
        group["excerpt"] = "\n".join(transitions)[:800] or None
        group["metadata"]["screen_transition_count"] = len(group["evidence_ids"])
    return sorted(
        groups, key=lambda item: (item["started_at"], item["evidence_ids"][0])
    )


def build_context_blocks(
    manifest: TimelineEvidenceManifest,
    *,
    max_chars: int = DEFAULT_BLOCK_MAX_CHARS,
    max_items: int = DEFAULT_BLOCK_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Assign every evidence item once to bounded chronological input blocks."""

    if max_chars <= 0 or max_items <= 0:
        raise ValueError("context block limits must be positive")
    blocks: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    size = 0

    def flush() -> None:
        nonlocal entries, size
        if not entries:
            return
        blocks.append(
            {
                "block_id": f"context-{len(blocks):04d}",
                "started_at": min(item["started_at"] for item in entries),
                "ended_at": max(item["ended_at"] for item in entries),
                "evidence": entries,
            }
        )
        entries = []
        size = 0

    for entry in compact_evidence_groups(manifest):
        entry_size = len(json.dumps(entry, default=str, separators=(",", ":")))
        if entries and (len(entries) >= max_items or size + entry_size > max_chars):
            flush()
        entries.append(entry)
        size += entry_size
    flush()
    return blocks


def is_dense_context_block(
    block: dict[str, Any],
    *,
    min_chars: int = DEFAULT_DENSE_MIN_CHARS,
    min_items: int = DEFAULT_DENSE_MIN_ITEMS,
) -> bool:
    evidence = block.get("evidence") or []
    size = len(json.dumps(block, default=str, separators=(",", ":")))
    source_items = sum(len(item.get("evidence_ids") or []) for item in evidence)
    return source_items >= min_items or size >= min_chars


def passthrough_context_summary(block: dict[str, Any]) -> TimelineContextSummary:
    """Sparse blocks need no model call; preserve each compact item as one event."""

    return TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                summary=(
                    item.get("excerpt")
                    or " · ".join(
                        str(value)
                        for value in (item.get("metadata") or {}).values()
                        if value not in (None, "", [], {})
                    )
                    or f"{item['kind']} evidence"
                )[:5000],
                evidence_ids=item["evidence_ids"],
                modalities=[item["kind"]],
                entities=list((item.get("metadata") or {}).get("speakers") or []),
                image_evidence_ids=item.get("image_evidence_ids") or [],
            )
            for item in block.get("evidence") or []
        ]
    )


def condenser_context_payload(
    block: dict[str, Any], *, max_representative_ids: int = 3
) -> dict[str, Any]:
    """Bound ID transport while retaining exact groups for deterministic repair.

    A five-minute screen bundle can contain hundreds of immutable observation IDs.
    The local model needs representative boundary/image IDs to ground its summary,
    not an instruction to copy every identifier into its response. The original
    ``block`` remains authoritative and :func:`repair_context_summary` expands any
    cited representative back to its complete source group.
    """

    if max_representative_ids <= 0:
        raise ValueError("max_representative_ids must be positive")
    evidence: list[dict[str, Any]] = []
    for item in block.get("evidence") or []:
        source_ids = list(dict.fromkeys(item.get("evidence_ids") or []))
        image_ids = [
            value
            for value in dict.fromkeys(item.get("image_evidence_ids") or [])
            if value in source_ids
        ]
        candidates = [
            *(source_ids[:1]),
            *(image_ids[:1]),
            *(source_ids[-1:]),
        ]
        selected = list(dict.fromkeys(candidates))[:max_representative_ids]
        compact = dict(item)
        compact["evidence_ids"] = selected
        compact["image_evidence_ids"] = [
            value for value in image_ids if value in selected
        ]
        compact["source_evidence_count"] = len(source_ids)
        evidence.append(compact)
    return {
        "block_id": block.get("block_id"),
        "started_at": block.get("started_at"),
        "ended_at": block.get("ended_at"),
        "evidence": evidence,
    }


def repair_context_summary(
    block: dict[str, Any], summary: TimelineContextSummary
) -> tuple[TimelineContextSummary, list[str]]:
    """Remove invented IDs and restore any source evidence the condenser omitted."""

    groups = list(block.get("evidence") or [])
    evidence = {
        evidence_id: item
        for item in groups
        for evidence_id in item.get("evidence_ids") or []
    }
    warnings: list[str] = []
    repaired: list[TimelineContextEvent] = []
    cited: set[str] = set()
    for event in summary.events:
        requested = list(dict.fromkeys(event.evidence_ids))
        requested_known = [item for item in requested if item in evidence]
        unknown = sorted(set(event.evidence_ids) - evidence.keys())
        if unknown:
            warnings.append(f"removed unknown evidence IDs: {unknown}")
        event_groups: list[dict[str, Any]] = []
        seen_groups: set[int] = set()
        for evidence_id in requested_known:
            group = evidence[evidence_id]
            identity = id(group)
            if identity not in seen_groups:
                seen_groups.add(identity)
                event_groups.append(group)
        known = [
            evidence_id
            for group in event_groups
            for evidence_id in group.get("evidence_ids") or []
            if evidence_id not in cited
        ]
        if not known:
            continue
        expanded = len(set(known) - set(requested_known))
        if expanded:
            warnings.append(f"expanded {expanded} grouped evidence IDs")
        temporal_groups = _temporal_batches(
            event_groups, max_items=max(1, len(event_groups))
        )
        if len(temporal_groups) > 1:
            warnings.append(
                f"split condenser event across {len(temporal_groups)} evidence islands"
            )
        for group_batch in temporal_groups:
            batch_ids = [
                evidence_id
                for group in group_batch
                for evidence_id in group.get("evidence_ids") or []
                if evidence_id not in cited
            ]
            if not batch_ids:
                continue
            repaired.append(
                event.model_copy(
                    update={
                        # Condenser bounds are advisory. Re-anchor each repaired event
                        # to its actual source groups so a semantic summary cannot turn
                        # two sparse islands into one continuous transport interval.
                        "started_at": min(
                            _context_time(group["started_at"]) for group in group_batch
                        ),
                        "ended_at": max(
                            _context_time(group["ended_at"]) for group in group_batch
                        ),
                        "evidence_ids": batch_ids,
                        "modalities": list(
                            dict.fromkeys(str(group["kind"]) for group in group_batch)
                        ),
                        "image_evidence_ids": list(
                            dict.fromkeys(
                                evidence_id
                                for group in group_batch
                                for evidence_id in group.get("image_evidence_ids") or []
                                if evidence_id in batch_ids
                            )
                        ),
                    }
                )
            )
            cited.update(batch_ids)
    unresolved = [
        item
        for item in dict.fromkeys(summary.unresolved_evidence_ids)
        if item in evidence
    ]
    missing = [
        evidence_id
        for group in groups
        for evidence_id in group.get("evidence_ids") or []
        if evidence_id not in cited
    ]
    fallback_ids = list(dict.fromkeys([*unresolved, *missing]))
    if fallback_ids:
        if missing:
            warnings.append(f"restored {len(missing)} omitted evidence IDs")
        fallback_groups: list[dict[str, Any]] = []
        seen_groups: set[int] = set()
        for evidence_id in fallback_ids:
            group = evidence[evidence_id]
            identity = id(group)
            if identity not in seen_groups:
                seen_groups.add(identity)
                fallback_groups.append(group)
        repaired.extend(_bounded_fallback_events(fallback_groups))
    return (
        TimelineContextSummary(
            events=sorted(repaired, key=lambda item: (item.started_at, item.ended_at)),
            unresolved_evidence_ids=unresolved,
        ),
        warnings,
    )


def _bounded_fallback_events(
    groups: list[dict[str, Any]], *, max_events: int = _MAX_FALLBACK_EVENTS
) -> list[TimelineContextEvent]:
    """Keep omitted evidence visible without exploding the final-day prompt.

    The condenser is asked for at most twelve semantic events, but evidence-integrity
    repair used to append one passthrough event for every omitted source group. On a
    dense day that turned 15 summaries into 706 events and a 183,957-token final
    prompt. These are transport fallback records, not semantic boundaries, so bundle
    adjacent groups while retaining their internal timestamped snippets and every
    authoritative evidence ID for deterministic coverage accounting.
    """

    if max_events <= 0:
        raise ValueError("max fallback context events must be positive")
    ordered = sorted(
        groups,
        key=lambda item: (
            _context_time(item["started_at"]),
            _context_time(item["ended_at"]),
            tuple(item.get("evidence_ids") or []),
        ),
    )
    if len(ordered) <= max_events:
        return passthrough_context_summary({"evidence": ordered}).events

    batch_size = math.ceil(len(ordered) / max_events)
    events: list[TimelineContextEvent] = []
    for batch in _temporal_batches(ordered, max_items=batch_size):
        snippets = []
        for item in batch:
            started_at = datetime.fromisoformat(
                str(item["started_at"]).replace("Z", "+00:00")
            )
            ended_at = datetime.fromisoformat(
                str(item["ended_at"]).replace("Z", "+00:00")
            )
            semantic_text = (
                item.get("excerpt")
                or " · ".join(
                    str(value)
                    for value in (item.get("metadata") or {}).values()
                    if value not in (None, "", [], {})
                )
                or f"{item['kind']} evidence"
            )
            snippets.append(
                f"{started_at:%H:%M:%S}–{ended_at:%H:%M:%S} "
                f"{str(semantic_text)[:_FALLBACK_SNIPPET_CHARS]}"
            )
        events.append(
            TimelineContextEvent(
                started_at=min(item["started_at"] for item in batch),
                ended_at=max(item["ended_at"] for item in batch),
                summary="\n".join(snippets),
                evidence_ids=[
                    evidence_id
                    for item in batch
                    for evidence_id in item.get("evidence_ids") or []
                ],
                modalities=list(dict.fromkeys(str(item["kind"]) for item in batch)),
                entities=list(
                    dict.fromkeys(
                        str(speaker)
                        for item in batch
                        for speaker in (item.get("metadata") or {}).get("speakers")
                        or []
                    )
                ),
                image_evidence_ids=[
                    evidence_id
                    for item in batch
                    for evidence_id in item.get("image_evidence_ids") or []
                ],
            )
        )
    return events


def _context_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _temporal_batches(items: list[Any], *, max_items: int) -> list[list[Any]]:
    """Bound transport without manufacturing continuity across empty time.

    The previous count-only batches joined whichever groups happened to straddle a
    batch boundary. On a sparse day that produced one context event from 00:26 to
    11:32, and the final model faithfully turned those unrelated islands into an
    eleven-hour episode. The event cap is therefore soft: a real gap or an hour of
    elapsed time starts another transport event even when that yields more batches.
    """

    if max_items <= 0:
        raise ValueError("temporal batch size must be positive")
    ordered = sorted(
        items,
        key=lambda item: (
            _context_time(
                item["started_at"] if isinstance(item, dict) else item.started_at
            ),
            _context_time(
                item["ended_at"] if isinstance(item, dict) else item.ended_at
            ),
        ),
    )
    batches: list[list[Any]] = []
    current: list[Any] = []
    batch_start: datetime | None = None
    covered_until: datetime | None = None
    for item in ordered:
        started_at = _context_time(
            item["started_at"] if isinstance(item, dict) else item.started_at
        )
        ended_at = _context_time(
            item["ended_at"] if isinstance(item, dict) else item.ended_at
        )
        if ended_at < started_at:
            ended_at = started_at
        split = bool(
            current
            and (
                len(current) >= max_items
                or started_at - (covered_until or started_at) > _MAX_BUNDLED_GAP
                or max(covered_until or ended_at, ended_at)
                - (batch_start or started_at)
                > _MAX_BUNDLED_SPAN
            )
        )
        if split:
            batches.append(current)
            current = []
            batch_start = None
            covered_until = None
        if not current:
            batch_start = started_at
            covered_until = ended_at
        else:
            covered_until = max(covered_until or ended_at, ended_at)
        current.append(item)
    if current:
        batches.append(current)
    return batches


def _bounded_summary_events(
    events: list[TimelineContextEvent],
    *,
    max_events: int = _MAX_FINAL_EVENTS_PER_BLOCK,
) -> list[TimelineContextEvent]:
    """Bound final transport even when an older cache already contains exploded fallbacks."""

    if max_events <= 0:
        raise ValueError("max final context events must be positive")
    ordered = sorted(events, key=lambda event: (event.started_at, event.ended_at))
    if len(ordered) <= max_events:
        return ordered

    bundled: list[TimelineContextEvent] = []
    max_items = math.ceil(len(ordered) / max_events)
    for batch in _temporal_batches(ordered, max_items=max_items):
        snippets = [
            f"{event.started_at:%H:%M:%S}–{event.ended_at:%H:%M:%S} "
            f"{event.summary[:_FALLBACK_SNIPPET_CHARS]}"
            for event in batch
        ]
        bundled.append(
            TimelineContextEvent(
                started_at=min(event.started_at for event in batch),
                ended_at=max(event.ended_at for event in batch),
                summary="\n".join(snippets),
                evidence_ids=list(
                    dict.fromkeys(
                        evidence_id
                        for event in batch
                        for evidence_id in event.evidence_ids
                    )
                ),
                modalities=list(
                    dict.fromkeys(
                        modality for event in batch for modality in event.modalities
                    )
                ),
                entities=list(
                    dict.fromkeys(
                        entity for event in batch for entity in event.entities
                    )
                ),
                image_evidence_ids=list(
                    dict.fromkeys(
                        evidence_id
                        for event in batch
                        for evidence_id in event.image_evidence_ids
                    )
                ),
            )
        )
    return bundled


def final_context_payload(
    block: dict[str, Any],
    summary: TimelineContextSummary,
    *,
    condensed: bool,
    max_citations_per_event: int = 8,
) -> dict[str, Any]:
    """Strip condenser bookkeeping while retaining boundary-supporting citations."""

    evidence = {
        evidence_id: item
        for item in block.get("evidence") or []
        for evidence_id in item.get("evidence_ids") or []
    }
    events: list[dict[str, Any]] = []
    for event in _bounded_summary_events(summary.events):
        ordered = sorted(
            dict.fromkeys(item for item in event.evidence_ids if item in evidence),
            key=lambda item: (
                evidence[item]["started_at"],
                evidence[item]["ended_at"],
                item,
            ),
        )
        if len(ordered) <= max_citations_per_event:
            selected = ordered
        else:
            images = [item for item in event.image_evidence_ids if item in ordered][:2]
            selected = list(dict.fromkeys([*ordered[:3], *images, *ordered[-3:]]))[
                :max_citations_per_event
            ]
        payload = event.model_dump(mode="json")
        payload["source_evidence_count"] = len(ordered)
        payload["evidence_ids"] = selected
        payload["image_evidence_ids"] = [
            item for item in event.image_evidence_ids if item in selected
        ]
        if condensed:
            payload["summary"] = payload["summary"][:1200]
        events.append(payload)
    return {
        "events": events,
        "unresolved_evidence_count": len(summary.unresolved_evidence_ids),
    }
