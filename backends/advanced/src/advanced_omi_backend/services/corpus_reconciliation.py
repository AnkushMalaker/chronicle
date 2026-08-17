"""Deterministic reconciliation of finite legacy/import conversation records.

This module decides identity; it never mutates MongoDB or the vault.  Its JSON
manifest is deliberately small enough to review and is consumed by import/cutover
commands.  Live capture is outside this policy: two occurrences with identical audio
are still two occurrences unless they come from the finite recovery corpus.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

MANIFEST_VERSION = 1
WORD = re.compile(r"[a-z0-9]+")


def normalize_transcript(value: str) -> str:
    return " ".join(WORD.findall(value.casefold()))


def pcm_identity(
    pcm: bytes, sample_rate: int, channels: int, sample_width: int = 2
) -> str:
    """Canonical identity for decoded finite audio, including its PCM format."""
    header = f"pcm-s{sample_width * 8}le:{sample_rate}:{channels}\0".encode()
    return hashlib.sha256(header + pcm).hexdigest()


def encoded_identity(data: bytes) -> str:
    """Integrity identity for one stored encoded chunk; never a dedupe key."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ReconciliationRecord:
    conversation_id: str
    user_id: str
    transcript: str = ""
    pcm_sha256: str | None = None
    audio_duration_seconds: float | None = None
    has_audio: bool = False
    data_purpose: str = "normal_capture"
    deleted: bool = False
    source: str = "legacy_backup"
    created_at: str | None = None
    transcript_provider: str | None = None


def _fivegrams(tokens: list[str]) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + 5]) for i in range(max(0, len(tokens) - 4))}


def conservative_transcript_match(
    left: str, right: str
) -> tuple[bool, dict[str, float]]:
    a, b = normalize_transcript(left), normalize_transcript(right)
    at, bt = a.split(), b.split()
    ratio = min(len(at), len(bt)) / max(len(at), len(bt), 1)
    ag, bg = _fivegrams(at), _fivegrams(bt)
    coverage = len(ag & bg) / max(1, min(len(ag), len(bg)))
    # SequenceMatcher is quadratic on long transcripts. Cheap gates reject almost
    # every unrelated corpus pair before invoking it.
    sequence = (
        SequenceMatcher(None, a, b, autojunk=False).ratio()
        if ratio >= 0.90 and coverage >= 0.80
        else 0.0
    )
    scores = {
        "sequence_similarity": sequence,
        "token_count_ratio": ratio,
        "fivegram_coverage": coverage,
    }
    return sequence >= 0.95 and ratio >= 0.90 and coverage >= 0.80, scores


def _rank(record: ReconciliationRecord) -> tuple[Any, ...]:
    # Normal semantic records and surviving audio beat derived/retired projections.
    return (
        record.data_purpose != "normal_capture",
        not record.has_audio,
        record.deleted,
        record.created_at or "9999",
        -len(normalize_transcript(record.transcript)),
        record.conversation_id,
    )


def build_manifest(records: Iterable[ReconciliationRecord]) -> dict[str, Any]:
    rows = list(records)
    by_id = {row.conversation_id: row for row in rows}
    canonical_by_source = {row.conversation_id: row.conversation_id for row in rows}
    decisions: list[dict[str, Any]] = []

    audio_groups: dict[tuple[str, str], list[ReconciliationRecord]] = {}
    for row in rows:
        if row.pcm_sha256:
            audio_groups.setdefault((row.user_id, row.pcm_sha256), []).append(row)
    for (_, digest), group in sorted(audio_groups.items()):
        if len(group) < 2:
            continue
        canonical = min(group, key=_rank)
        for duplicate in sorted(group, key=_rank):
            canonical_by_source[duplicate.conversation_id] = canonical.conversation_id
        decisions.append(
            {
                "basis": "exact_pcm_sha256",
                "canonical_id": canonical.conversation_id,
                "source_ids": sorted(item.conversation_id for item in group),
                "pcm_sha256": digest,
                "selected_transcript_source_id": min(
                    group,
                    key=lambda item: (
                        item.transcript_provider != "smallest",
                        not bool(item.transcript),
                        -len(normalize_transcript(item.transcript)),
                        item.conversation_id,
                    ),
                ).conversation_id,
            }
        )

    canonicals = [
        row
        for row in rows
        if canonical_by_source[row.conversation_id] == row.conversation_id
    ]
    blockers: list[dict[str, Any]] = []
    for alias in rows:
        if alias.has_audio or not normalize_transcript(alias.transcript):
            continue
        exact = [
            candidate
            for candidate in canonicals
            if candidate.user_id == alias.user_id
            and candidate.has_audio
            and normalize_transcript(candidate.transcript)
            == normalize_transcript(alias.transcript)
        ]
        basis = "exact_normalized_transcript"
        scored: list[tuple[ReconciliationRecord, dict[str, float]]] = [
            (
                candidate,
                {
                    "sequence_similarity": 1.0,
                    "token_count_ratio": 1.0,
                    "fivegram_coverage": 1.0,
                },
            )
            for candidate in exact
        ]
        if not scored:
            basis = "conservative_transcript"
            for candidate in canonicals:
                if candidate.user_id != alias.user_id or not candidate.has_audio:
                    continue
                matched, scores = conservative_transcript_match(
                    alias.transcript, candidate.transcript
                )
                if matched:
                    scored.append((candidate, scores))
        targets = {
            canonical_by_source[candidate.conversation_id] for candidate, _ in scored
        }
        if len(targets) == 1:
            target = next(iter(targets))
            if target != alias.conversation_id:
                canonical_by_source[alias.conversation_id] = target
                decisions.append(
                    {
                        "basis": basis,
                        "canonical_id": target,
                        "source_ids": [alias.conversation_id, target],
                        "scores": scored[0][1],
                    }
                )
        elif len(targets) > 1:
            blockers.append(
                {
                    "conversation_id": alias.conversation_id,
                    "reason": "ambiguous_transcript_alias",
                    "candidate_ids": sorted(targets),
                }
            )

    retired = sorted(
        source
        for source, canonical in canonical_by_source.items()
        if source != canonical
    )
    return {
        "manifest_version": MANIFEST_VERSION,
        "canonical_by_source": dict(sorted(canonical_by_source.items())),
        "retired_source_ids": retired,
        "decisions": decisions,
        "blockers": blockers,
        "activation_allowed": not blockers,
        "records": [asdict(by_id[key]) for key in sorted(by_id)],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"unsupported reconciliation manifest: {payload.get('manifest_version')}"
        )
    return payload
