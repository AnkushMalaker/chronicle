"""Read-only, shadow relation inference over a bounded Timeline evidence manifest.

This first slice deliberately emits only strong transcript corroboration and possible
input/output echo candidates.  It does not merge Conversations, mutate evidence,
publish Episodes, or claim that unlike transcripts conflict: independent speakers in
the same event routinely produce dissimilar text.

The lexical scorer is a tracer bullet, not the proposed production audio reconciler.
Its purpose is to make source resolution, deterministic relation identity, provenance,
and the preview API runnable while acoustic fingerprints and clock maps remain future
shadow stages.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .contracts import TimelineEvidenceItem, TimelineEvidenceManifest

ALGORITHM = "transcript-overlap-shadow-v1"
MIN_TOKENS = 8
MIN_TOKEN_CONTAINMENT = 0.72
MIN_BIGRAM_CONTAINMENT = 0.52
MIN_TEMPORAL_COVERAGE = 0.45
MAX_RESOLVED_TRANSCRIPTS = 500
MAX_CANDIDATE_PAIRS = 20_000

_SPEAKER_PREFIX = re.compile(r"(?m)^[^:\n]{1,80}:\s*")
_TOKEN = re.compile(r"[\w']+", re.UNICODE)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class EvidenceRelation(BaseModel):
    """One deterministic, non-authoritative relation candidate."""

    relation_id: str
    relation_type: Literal["corroborates", "possible_echo"]
    evidence_ids: tuple[str, str]
    source_ids: tuple[str, str]
    started_at: datetime
    ended_at: datetime
    score: float = Field(ge=0, le=1)
    calibrated: bool = False
    algorithm: str = ALGORITHM
    signals: dict[str, float | int | str | bool] = Field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EvidenceRelationPreview(BaseModel):
    """Read-only output returned by the shadow preview endpoint."""

    algorithm: str = ALGORITHM
    evidence_revision: str
    evidence_count: int
    transcript_count: int
    resolved_transcript_count: int
    compared_transcript_count: int
    unresolved_source_count: int
    candidate_pair_count: int
    relation_count: int
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    source_ids: list[str]
    relations: list[EvidenceRelation]


@dataclass(frozen=True)
class _TranscriptView:
    item: TimelineEvidenceItem
    source_id: str
    direction: str
    source_resolution: Literal["direct", "conversation_audio_span"]


def _conversation_sources(
    evidence: list[TimelineEvidenceItem],
) -> dict[str, set[tuple[str, str]]]:
    result: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    for item in evidence:
        if item.kind != "audio_span" or not item.source_id:
            continue
        conversation_id = str(item.metadata.get("conversation_id") or "")
        if not conversation_id:
            continue
        direction = str(item.metadata.get("direction") or "unknown")
        result[conversation_id].add((item.source_id, direction))
    return dict(result)


def _resolve_transcripts(
    evidence: list[TimelineEvidenceItem],
) -> tuple[list[_TranscriptView], int]:
    conversation_sources = _conversation_sources(evidence)
    result: list[_TranscriptView] = []
    unresolved = 0
    for item in evidence:
        if item.kind != "transcript" or not (item.excerpt or "").strip():
            continue
        direction = str(item.metadata.get("direction") or "unknown")
        if item.source_id:
            result.append(
                _TranscriptView(
                    item=item,
                    source_id=item.source_id,
                    direction=direction,
                    source_resolution="direct",
                )
            )
            continue

        conversation_id = str(item.metadata.get("conversation_id") or "")
        sources = conversation_sources.get(conversation_id, set())
        if len(sources) != 1:
            unresolved += 1
            continue
        source_id, audio_direction = next(iter(sources))
        result.append(
            _TranscriptView(
                item=item,
                source_id=source_id,
                direction=(direction if direction != "unknown" else audio_direction),
                source_resolution="conversation_audio_span",
            )
        )
    return result, unresolved


def _tokens(text: str) -> tuple[str, ...]:
    without_labels = _SPEAKER_PREFIX.sub("", text.lower())
    return tuple(
        token
        for token in _TOKEN.findall(without_labels)
        if len(token) > 1 and not token.isdigit()
    )[:4_000]


def _multiset_containment(
    left: tuple[str, ...], right: tuple[str, ...]
) -> tuple[float, int]:
    left_counts, right_counts = Counter(left), Counter(right)
    shared = sum((left_counts & right_counts).values())
    denominator = min(len(left), len(right))
    return (shared / denominator if denominator else 0.0), shared


def _bigrams(tokens: tuple[str, ...]) -> Counter[tuple[str, str]]:
    return Counter(zip(tokens, tokens[1:]))


def _bigram_containment(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_bigrams, right_bigrams = _bigrams(left), _bigrams(right)
    denominator = min(sum(left_bigrams.values()), sum(right_bigrams.values()))
    if denominator <= 0:
        return 0.0
    return sum((left_bigrams & right_bigrams).values()) / denominator


def _interval_signals(
    left: TimelineEvidenceItem, right: TimelineEvidenceItem
) -> tuple[datetime, datetime, float, float] | None:
    if left.ended_at is None or right.ended_at is None:
        return None
    left_start, left_end = _utc(left.started_at), _utc(left.ended_at)
    right_start, right_end = _utc(right.started_at), _utc(right.ended_at)
    started_at = max(left_start, right_start)
    ended_at = min(left_end, right_end)
    if ended_at <= started_at:
        return None
    overlap = (ended_at - started_at).total_seconds()
    shorter = min(
        (left_end - left_start).total_seconds(),
        (right_end - right_start).total_seconds(),
    )
    union = (max(left_end, right_end) - min(left_start, right_start)).total_seconds()
    coverage = overlap / shorter if shorter > 0 else 0.0
    iou = overlap / union if union > 0 else 0.0
    return started_at, ended_at, coverage, iou


def _relation_id(
    left: _TranscriptView,
    right: _TranscriptView,
    relation_type: str,
) -> str:
    pair = sorted(
        (
            (
                left.item.evidence_id,
                left.source_id,
                left.item.content_hash or "",
            ),
            (
                right.item.evidence_id,
                right.source_id,
                right.item.content_hash or "",
            ),
        )
    )
    payload = "\x1f".join(
        [
            ALGORITHM,
            relation_type,
            *(part for item in pair for part in item),
        ]
    )
    return "evidence-relation:" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _ordered_pair(
    left: _TranscriptView, right: _TranscriptView
) -> tuple[_TranscriptView, _TranscriptView]:
    return (
        (left, right)
        if left.item.evidence_id <= right.item.evidence_id
        else (right, left)
    )


def _relation(
    left: _TranscriptView,
    right: _TranscriptView,
    interval: tuple[datetime, datetime, float, float],
    left_tokens: tuple[str, ...],
    right_tokens: tuple[str, ...],
) -> EvidenceRelation | None:
    if min(len(left_tokens), len(right_tokens)) < MIN_TOKENS:
        return None

    token_containment, shared_tokens = _multiset_containment(left_tokens, right_tokens)
    bigram_containment = _bigram_containment(left_tokens, right_tokens)
    started_at, ended_at, temporal_coverage, interval_iou = interval
    if (
        token_containment < MIN_TOKEN_CONTAINMENT
        or bigram_containment < MIN_BIGRAM_CONTAINMENT
        or temporal_coverage < MIN_TEMPORAL_COVERAGE
    ):
        return None

    directions = {left.direction, right.direction}
    relation_type = (
        "possible_echo" if directions == {"input", "output"} else "corroborates"
    )
    score = min(
        1.0,
        0.45 * token_containment + 0.35 * bigram_containment + 0.20 * temporal_coverage,
    )
    ordered_left, ordered_right = _ordered_pair(left, right)
    warnings: list[str] = [
        "shadow lexical score is not calibrated and cannot authorize merging"
    ]
    if {
        left.source_resolution,
        right.source_resolution,
    } != {"direct"}:
        warnings.append(
            "source identity inferred through conversation-linked audio_span"
        )
    if relation_type == "possible_echo":
        warnings.append(
            "input/output agreement is only an echo candidate until acoustic verification"
        )
    return EvidenceRelation(
        relation_id=_relation_id(left, right, relation_type),
        relation_type=relation_type,
        evidence_ids=(
            ordered_left.item.evidence_id,
            ordered_right.item.evidence_id,
        ),
        source_ids=(ordered_left.source_id, ordered_right.source_id),
        started_at=started_at,
        ended_at=ended_at,
        score=score,
        signals={
            "token_containment": round(token_containment, 6),
            "bigram_containment": round(bigram_containment, 6),
            "shared_tokens": shared_tokens,
            "temporal_coverage": round(temporal_coverage, 6),
            "interval_iou": round(interval_iou, 6),
            "left_direction": ordered_left.direction,
            "right_direction": ordered_right.direction,
            "same_transcript_hash": bool(
                left.item.content_hash
                and left.item.content_hash == right.item.content_hash
            ),
            "left_source_resolution": ordered_left.source_resolution,
            "right_source_resolution": ordered_right.source_resolution,
        },
        reason_codes=(
            "strong_monotonic_lexical_overlap",
            "clock_interval_overlap",
            (
                "opposite_capture_directions"
                if relation_type == "possible_echo"
                else "independent_source_corroboration"
            ),
        ),
        warnings=tuple(warnings),
    )


def infer_evidence_relations(
    manifest: TimelineEvidenceManifest,
) -> EvidenceRelationPreview:
    """Infer high-precision shadow candidates without changing Chronicle state."""

    all_transcripts, unresolved = _resolve_transcripts(manifest.evidence)
    all_transcripts.sort(
        key=lambda view: (_utc(view.item.started_at), view.item.evidence_id)
    )
    transcripts = all_transcripts[:MAX_RESOLVED_TRANSCRIPTS]
    truncated = len(all_transcripts) > len(transcripts)
    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"comparison capped at {MAX_RESOLVED_TRANSCRIPTS} resolved transcripts"
        )
    relations: list[EvidenceRelation] = []
    token_cache = {
        view.item.evidence_id: _tokens(view.item.excerpt or "") for view in transcripts
    }
    candidate_pairs = 0
    candidate_cap_reached = False
    for index, left in enumerate(transcripts):
        for right in transcripts[index + 1 :]:
            if left.source_id == right.source_id:
                continue
            interval = _interval_signals(left.item, right.item)
            if interval is None:
                continue
            if candidate_pairs >= MAX_CANDIDATE_PAIRS:
                candidate_cap_reached = True
                break
            candidate_pairs += 1
            relation = _relation(
                left,
                right,
                interval,
                token_cache[left.item.evidence_id],
                token_cache[right.item.evidence_id],
            )
            if relation is not None:
                relations.append(relation)
        if candidate_cap_reached:
            break
    if candidate_cap_reached:
        truncated = True
        warnings.append(
            f"comparison capped at {MAX_CANDIDATE_PAIRS} overlapping source pairs"
        )

    relations.sort(key=lambda item: (item.started_at, item.relation_id))
    return EvidenceRelationPreview(
        evidence_revision=manifest.evidence_revision,
        evidence_count=len(manifest.evidence),
        transcript_count=sum(item.kind == "transcript" for item in manifest.evidence),
        resolved_transcript_count=len(all_transcripts),
        compared_transcript_count=len(transcripts),
        unresolved_source_count=unresolved,
        candidate_pair_count=candidate_pairs,
        relation_count=len(relations),
        truncated=truncated,
        warnings=warnings,
        source_ids=sorted({view.source_id for view in all_transcripts}),
        relations=relations,
    )
