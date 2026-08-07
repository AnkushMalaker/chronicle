"""Validation and executor selection for semantic timeline analysis."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import load_config

from .codex_executor import CodexTimelineExecutor
from .contracts import TimelineAgentResult, TimelineEvidenceManifest, UnassignedInterval

logger = logging.getLogger(__name__)

BOUNDARY_SUPPORT_TOLERANCE = timedelta(minutes=2)
ACCOUNTING_GAP_TOLERANCE = timedelta(minutes=1)


def _evidence_end(item) -> datetime:
    return item.ended_at or item.started_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _supports_boundary(item, boundary: datetime) -> bool:
    return (
        item.started_at - BOUNDARY_SUPPORT_TOLERANCE
        <= boundary
        <= _evidence_end(item) + BOUNDARY_SUPPORT_TOLERANCE
    )


def _accounted_intervals(
    result: TimelineAgentResult,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Intervals the day is already explained by.

    Confirmed episodes carried over from an earlier generation are pinned: the agent is
    told not to re-segment them, so their evidence must not read as unexplained.
    """

    return sorted(
        [
            (interval.started_at, interval.ended_at)
            for interval in result.unassigned_intervals
        ]
        + [(episode.started_at, episode.ended_at) for episode in result.episodes]
        + list(pinned_intervals or []),
        key=lambda interval: interval[0],
    )


def _validate_evidence_accounting(
    result: TimelineAgentResult,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> None:
    accounted = _accounted_intervals(result, pinned_intervals)
    for index, item in enumerate(manifest.evidence):
        start = max(item.started_at, manifest.started_at)
        end = min(_evidence_end(item), manifest.ended_at)
        if end <= start:
            if not any(
                low - ACCOUNTING_GAP_TOLERANCE
                <= start
                <= high + ACCOUNTING_GAP_TOLERANCE
                for low, high in accounted
            ):
                raise ValueError(f"unaccounted evidence interval at item {index}")
            continue
        cursor = start
        for low, high in accounted:
            if high <= cursor:
                continue
            if low > cursor:
                if low - cursor > ACCOUNTING_GAP_TOLERANCE:
                    break
                cursor = low
            cursor = max(cursor, min(high, end))
            if cursor >= end:
                break
        if end - cursor > ACCOUNTING_GAP_TOLERANCE:
            raise ValueError(f"unaccounted evidence interval at item {index}")


def _fill_unassigned_evidence(
    result: TimelineAgentResult,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> None:
    """Materialize evidence the semantic draft did not explain as explicit unknowns."""

    accounted = _accounted_intervals(result, pinned_intervals)
    missing: list[tuple[datetime, datetime]] = []
    point_width = timedelta(seconds=1)
    for item in manifest.evidence:
        start = max(item.started_at, manifest.started_at)
        end = min(_evidence_end(item), manifest.ended_at)
        if end <= start:
            if any(
                low - ACCOUNTING_GAP_TOLERANCE
                <= start
                <= high + ACCOUNTING_GAP_TOLERANCE
                for low, high in accounted
            ):
                continue
            low = max(manifest.started_at, start - point_width)
            high = min(manifest.ended_at, start + point_width)
            if high > low:
                missing.append((low, high))
            continue

        cursor = start
        for low, high in accounted:
            if high <= cursor:
                continue
            if low > cursor:
                gap_end = min(low, end)
                if gap_end - cursor > ACCOUNTING_GAP_TOLERANCE:
                    missing.append((cursor, gap_end))
            cursor = max(cursor, min(high, end))
            if cursor >= end:
                break
        if end - cursor > ACCOUNTING_GAP_TOLERANCE:
            missing.append((cursor, end))

    merged: list[tuple[datetime, datetime]] = []
    for low, high in sorted(missing):
        if merged and low <= merged[-1][1] + ACCOUNTING_GAP_TOLERANCE:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    result.unassigned_intervals.extend(
        UnassignedInterval(
            started_at=low,
            ended_at=high,
            reason="Evidence was not assigned by semantic analysis",
        )
        for low, high in merged
    )


def _classify_unassigned_intervals(
    result: TimelineAgentResult, manifest: TimelineEvidenceManifest
) -> None:
    """Decide *why* each unassigned interval is unassigned, from the manifest.

    `capture_gap` items record absent capture, so they do not count as evidence that
    something happened. An interval overlapped by any other evidence kind was captured
    and simply not explained; an interval overlapped by nothing else had nothing to
    explain.
    """

    captured = [
        (
            max(item.started_at, manifest.started_at),
            min(_evidence_end(item), manifest.ended_at),
        )
        for item in manifest.evidence
        if item.kind != "capture_gap"
    ]
    for interval in result.unassigned_intervals:
        start, end = _utc(interval.started_at), _utc(interval.ended_at)
        interval.cause = (
            "unexplained"
            if any(_utc(low) < end and start < _utc(high) for low, high in captured)
            else "no_capture"
        )


def _drop_pinned_duplicates(
    result: TimelineAgentResult, pinned_intervals: list[tuple[datetime, datetime]]
) -> None:
    """Discard drafted episodes that re-describe a confirmed interval.

    The prompt tells the agent to leave pinned intervals alone, but a stray overlap must
    not surface as two episodes for the same stretch of the day. Runs after episode
    bounds are clamped to cited evidence, since clamping can pull a draft that started
    outside a pinned interval into it.
    """

    keep: list[int] = []
    for index, episode in enumerate(result.episodes):
        started, ended = _utc(episode.started_at), _utc(episode.ended_at)
        span = (ended - started).total_seconds()
        covered = sum(
            max(
                0.0,
                (min(ended, _utc(high)) - max(started, _utc(low))).total_seconds(),
            )
            for low, high in pinned_intervals
        )
        if span > 0 and covered / span > 0.5:
            continue
        keep.append(index)
    if len(keep) == len(result.episodes):
        return
    remap = {old: new for new, old in enumerate(keep)}
    result.episodes = [result.episodes[index] for index in keep]
    for episode in result.episodes:
        if episode.parent_episode_index is not None:
            episode.parent_episode_index = remap.get(episode.parent_episode_index)


class TimelineIncompleteSegmentation(RuntimeError):
    """The agent returned no account of a day that has evidence.

    Restores the check removed in "fix(timeline): bound incomplete Luna output", which
    made an empty result *acceptable* and silently materialized it as unassigned time.
    That turned a model failure into a successful-looking run that blanked the day. An
    empty result is a retryable failure, not an answer: even a wholly idle day should
    come back as an idle episode or an explicit unassigned interval.
    """


def validate_agent_result(
    result: TimelineAgentResult,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> None:
    # Checked before _fill_unassigned_evidence, which would otherwise manufacture the
    # very intervals whose absence proves the agent said nothing.
    #
    # Skipped when episodes are pinned: a day already accounted for by confirmed
    # episodes legitimately leaves the agent nothing to add. Partial coverage is still
    # caught — _validate_evidence_accounting rejects evidence no interval explains.
    if (
        manifest.evidence
        and not result.episodes
        and not result.unassigned_intervals
        and not pinned_intervals
    ):
        raise TimelineIncompleteSegmentation(
            f"agent produced no episodes and no unassigned intervals for "
            f"{len(manifest.evidence)} evidence items across {len(manifest.windows)} windows"
        )
    bounded_unassigned: list[UnassignedInterval] = []
    for interval in result.unassigned_intervals:
        interval.started_at = max(_utc(interval.started_at), manifest.started_at)
        interval.ended_at = min(_utc(interval.ended_at), manifest.ended_at)
        if interval.ended_at > interval.started_at:
            bounded_unassigned.append(interval)
    result.unassigned_intervals = bounded_unassigned
    for episode in result.episodes:
        episode.started_at = _utc(episode.started_at)
        episode.ended_at = _utc(episode.ended_at)

    for index, interval in enumerate(result.unassigned_intervals):
        if interval.ended_at <= interval.started_at:
            raise ValueError(f"unassigned interval {index} must have positive duration")

    evidence = {item.evidence_id: item for item in manifest.evidence}
    # One malformed episode must not discard a whole day of good ones. Under
    # --output-schema the agent's own planning narration is schema-valid, so a stray
    # `{"kind": "task"}` entry can appear beside nine real episodes; rejecting the run
    # threw all of them away. Bad episodes are dropped individually, and the day still
    # fails loudly if nothing survives.
    kept: list[int] = []
    dropped: list[str] = []
    for index, episode in enumerate(result.episodes):
        if (
            episode.started_at < manifest.started_at
            or episode.ended_at > manifest.ended_at
        ):
            dropped.append(f"episode {index} lies outside the analyzed day range")
            continue
        unknown = set(episode.evidence_ids) - evidence.keys()
        if unknown:
            dropped.append(
                f"episode {index} references unknown evidence: {sorted(unknown)}"
            )
            continue
        overlapping_evidence_ids: list[str] = []
        for evidence_id in episode.evidence_ids:
            item = evidence[evidence_id]
            item_end = item.ended_at or item.started_at
            if item.started_at < episode.ended_at and item_end > episode.started_at:
                overlapping_evidence_ids.append(evidence_id)
        if not overlapping_evidence_ids:
            dropped.append(f"episode {index} has no temporally overlapping evidence")
            continue
        episode.evidence_ids = overlapping_evidence_ids
        cited_evidence = [evidence[evidence_id] for evidence_id in episode.evidence_ids]
        if not any(
            _supports_boundary(item, episode.started_at) for item in cited_evidence
        ):
            episode.started_at = min(item.started_at for item in cited_evidence)
        if not any(
            _supports_boundary(item, episode.ended_at) for item in cited_evidence
        ):
            episode.ended_at = max(_evidence_end(item) for item in cited_evidence)
        if episode.ended_at <= episode.started_at:
            dropped.append(
                f"episode {index} cannot be bounded to a positive cited interval"
            )
            continue
        bound_evidence = set(overlapping_evidence_ids)
        for assertion in episode.assertions:
            assertion.evidence_ids = [
                evidence_id
                for evidence_id in assertion.evidence_ids
                if evidence_id in bound_evidence
            ]
        episode.assertions = [
            assertion for assertion in episode.assertions if assertion.evidence_ids
        ]
        if episode.representative_evidence_id:
            representative = evidence.get(episode.representative_evidence_id)
            if (
                episode.representative_evidence_id not in episode.evidence_ids
                or representative is None
                or not representative.image_filename
            ):
                # A thumbnail is optional decoration. Drop a bad selection without
                # discarding otherwise valid semantic boundaries and citations.
                episode.representative_evidence_id = None
        if episode.parent_episode_index is not None and (
            episode.parent_episode_index >= len(result.episodes)
            or episode.parent_episode_index == index
        ):
            # Parenting is structure, not substance — unlink rather than lose the episode.
            episode.parent_episode_index = None
        kept.append(index)

    if dropped:
        logger.warning(
            "🧹 Dropped %d malformed episode(s) of %d, keeping %d: %s",
            len(dropped),
            len(result.episodes),
            len(kept),
            "; ".join(dropped[:5]),
        )
    if result.episodes and not kept:
        raise TimelineIncompleteSegmentation(
            f"every one of {len(result.episodes)} drafted episodes was malformed: "
            + "; ".join(dropped[:3])
        )
    if dropped:
        remap = {old: new for new, old in enumerate(kept)}
        result.episodes = [result.episodes[index] for index in kept]
        for episode in result.episodes:
            if episode.parent_episode_index is not None:
                episode.parent_episode_index = remap.get(episode.parent_episode_index)

    if pinned_intervals:
        _drop_pinned_duplicates(result, pinned_intervals)
    _fill_unassigned_evidence(result, manifest, pinned_intervals)
    _classify_unassigned_intervals(result, manifest)
    _validate_evidence_accounting(result, manifest, pinned_intervals)


def settings_dict() -> dict[str, Any]:
    value = load_config().get("timeline", {})
    converted = OmegaConf.to_container(value, resolve=True)
    return converted if isinstance(converted, dict) else {}


def build_executor():
    settings = settings_dict()
    executor = str(settings.get("executor") or "codex")
    if executor != "codex":
        raise ValueError(f"unsupported timeline executor: {executor}")

    return CodexTimelineExecutor(settings.get("codex") or {})
