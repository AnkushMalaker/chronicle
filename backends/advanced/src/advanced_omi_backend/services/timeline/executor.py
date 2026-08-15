"""Validation and executor selection for semantic timeline analysis."""

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import load_config

from .codex_executor import CodexTimelineExecutor
from .contracts import (
    EvidenceBundle,
    Publish,
    ReconcileAction,
    RequestMoreContext,
    TimelineAgentResult,
    TimelineEvidenceManifest,
    UnassignedInterval,
    WaitForFutureEvidence,
)
from .pi_executor import PiTimelineExecutor
from .workspace import write_workspace

logger = logging.getLogger(__name__)

BOUNDARY_SUPPORT_TOLERANCE = timedelta(minutes=2)
ACCOUNTING_GAP_TOLERANCE = timedelta(minutes=1)
EPISODE_EVIDENCE_GAP_TOLERANCE = timedelta(minutes=15)
BSON_DATETIME_PRECISION = timedelta(milliseconds=1)


def _evidence_end(item) -> datetime:
    return item.ended_at or item.started_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bson_datetime(value: datetime) -> datetime:
    """Normalize a timestamp to MongoDB BSON's millisecond precision."""

    value = _utc(value)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _supports_boundary(item, boundary: datetime) -> bool:
    return (
        item.started_at - BOUNDARY_SUPPORT_TOLERANCE
        <= boundary
        <= _evidence_end(item) + BOUNDARY_SUPPORT_TOLERANCE
    )


def unsupported_episode_gap(
    episode, evidence_items
) -> tuple[datetime, datetime] | None:
    """Return a large empty interval that an episode improperly bridges.

    Use all day evidence, not only the episode's bounded citation sample: compact
    context intentionally exposes representative IDs, so intermediate supplied items
    can prove continuity without every ID being echoed by the model.
    """

    episode_start = _utc(episode.started_at)
    episode_end = _utc(episode.ended_at)
    intervals: list[tuple[datetime, datetime]] = []
    for item in evidence_items:
        if item.kind == "capture_gap":
            continue
        low = max(_utc(item.started_at), episode_start)
        high = min(_utc(_evidence_end(item)), episode_end)
        if high <= low:
            continue
        intervals.append((low, high))
    covered_until: datetime | None = None
    for low, high in sorted(intervals):
        if (
            covered_until is not None
            and low - covered_until > EPISODE_EVIDENCE_GAP_TOLERANCE
        ):
            return covered_until, low
        covered_until = max(covered_until or high, high)
    return None


def _unique_evidence_suffixes(evidence_ids) -> dict[str, str]:
    """Map a model-shortened ID back only when the suffix is unambiguous."""

    candidates: dict[str, str] = {}
    ambiguous: set[str] = set()
    for evidence_id in evidence_ids:
        suffix = evidence_id.rsplit(":", 1)[-1]
        previous = candidates.get(suffix)
        if previous is not None and previous != evidence_id:
            ambiguous.add(suffix)
        else:
            candidates[suffix] = evidence_id
    return {
        suffix: evidence_id
        for suffix, evidence_id in candidates.items()
        if suffix not in ambiguous
    }


def _canonicalize_evidence_ids(values, evidence, suffixes) -> tuple[list[str], int]:
    repaired = [
        value if value in evidence else suffixes.get(value, value) for value in values
    ]
    unique = list(dict.fromkeys(repaired))
    return unique, sum(
        original != fixed for original, fixed in zip(values, repaired, strict=True)
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
    *,
    salvage_gap_bridging_episodes: bool = False,
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
    suffixes = _unique_evidence_suffixes(evidence)
    citation_repairs = 0
    for episode in result.episodes:
        episode.evidence_ids, repaired = _canonicalize_evidence_ids(
            episode.evidence_ids, evidence, suffixes
        )
        citation_repairs += repaired
        for assertion in episode.assertions:
            assertion.evidence_ids, repaired = _canonicalize_evidence_ids(
                assertion.evidence_ids, evidence, suffixes
            )
            citation_repairs += repaired
        if episode.representative_evidence_id not in (None, ""):
            canonical, repaired = _canonicalize_evidence_ids(
                [episode.representative_evidence_id], evidence, suffixes
            )
            episode.representative_evidence_id = canonical[0]
            citation_repairs += repaired
    if citation_repairs:
        logger.warning(
            "Restored evidence-kind prefixes on %d uniquely matched citation(s)",
            citation_repairs,
        )
    # One malformed episode must not discard a whole day of good ones. Under
    # --output-schema the agent's own planning narration is schema-valid, so a stray
    # `{"kind": "task"}` entry can appear beside nine real episodes; rejecting the run
    # threw all of them away. Bad episodes are dropped individually, and the day still
    # fails loudly if nothing survives.
    kept: list[int] = []
    dropped: list[str] = []
    removed_unknown_citations: list[str] = []
    for index, episode in enumerate(result.episodes):
        if (
            episode.started_at < manifest.started_at
            or episode.ended_at > manifest.ended_at
        ):
            dropped.append(f"episode {index} lies outside the analyzed day range")
            continue
        unknown = set(episode.evidence_ids) - evidence.keys()
        if unknown:
            known_evidence_ids = [
                evidence_id
                for evidence_id in episode.evidence_ids
                if evidence_id in evidence
            ]
            if not known_evidence_ids:
                dropped.append(
                    f"episode {index} references unknown evidence: {sorted(unknown)}"
                )
                continue
            episode.evidence_ids = known_evidence_ids
            removed_unknown_citations.append(f"episode {index}: {sorted(unknown)}")
        overlapping_evidence_ids: list[str] = []
        for evidence_id in episode.evidence_ids:
            item = evidence[evidence_id]
            item_end = item.ended_at or item.started_at
            # Evidence at an exact episode boundary is still grounding. Screen
            # observations commonly have zero duration, and condensed output uses
            # one observation as the start marker and the next as the end marker.
            # A strict half-open comparison discarded those otherwise exact spans.
            if item.started_at <= episode.ended_at and item_end >= episode.started_at:
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
        # BSON truncates datetimes to milliseconds. A genuinely positive point-event
        # draft such as .792974 -> .792975 passed Pydantic here but round-tripped from
        # Mongo as .792 -> .792, making the published TimelineEpisode unreadable.
        # Validate the persisted representation, extending only intervals that were
        # positive before truncation. One millisecond is the smallest truthful stored
        # span and remains inside the analyzed day.
        episode.started_at = _bson_datetime(episode.started_at)
        episode.ended_at = _bson_datetime(episode.ended_at)
        if episode.ended_at <= episode.started_at:
            extended_end = episode.started_at + BSON_DATETIME_PRECISION
            if extended_end > manifest.ended_at:
                dropped.append(f"episode {index} collapses at BSON datetime precision")
                continue
            episode.ended_at = extended_end
        unsupported_gap = unsupported_episode_gap(episode, manifest.evidence)
        if unsupported_gap is not None:
            low, high = unsupported_gap
            diagnostic = (
                f"episode {index} bridges an uncaptured internal gap of "
                f"{(high - low).total_seconds():.0f}s ({low.isoformat()} to "
                f"{high.isoformat()}); split the episode or leave the gap unassigned"
            )
            if not salvage_gap_bridging_episodes:
                raise TimelineIncompleteSegmentation(diagnostic)
            # After the model has seen this exact validation failure at lower efforts,
            # preserving one still-bridged episode would assert continuity through
            # time Chronicle did not capture. Drop only that draft; the ordinary
            # evidence-accounting pass below materializes its evidence islands as
            # unassigned intervals, while every independently valid episode survives.
            dropped.append(diagnostic)
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
    if removed_unknown_citations:
        logger.warning(
            "🧹 Removed unknown citations from %d otherwise valid episode(s): %s",
            len(removed_unknown_citations),
            "; ".join(removed_unknown_citations[:5]),
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
    converted = (
        OmegaConf.to_container(value, resolve=True)
        if OmegaConf.is_config(value)
        else value
    )
    return converted if isinstance(converted, dict) else {}


def build_executor():
    settings = settings_dict()
    executor = str(settings.get("executor") or "codex")
    if executor == "codex":
        return CodexTimelineExecutor(settings.get("codex") or {})
    if executor == "pi":
        return PiTimelineExecutor(settings.get("pi") or {})
    raise ValueError(f"unsupported timeline executor: {executor}")


# ── Range mode: the rolling reconciliation adapter ───────────────────────────


def parse_range_action(payload: dict[str, Any]) -> ReconcileAction:
    """Validate one range-mode agent answer into a :data:`ReconcileAction`.

    The action is explicit rather than inferred: an answer carrying no episodes is a
    model failure in day mode, and treating it as "nothing happened" here would be the
    same silent day-blanking bug in a new place.
    """

    action = str(payload.get("action") or "").strip()
    if action == "publish":
        return Publish(
            result=TimelineAgentResult.model_validate(
                {
                    "episodes": payload.get("episodes") or [],
                    "unassigned_intervals": payload.get("unassigned_intervals") or [],
                }
            )
        )
    if action == "request_more_context":
        return RequestMoreContext(
            left_seconds=float(payload.get("left_seconds") or 0),
            right_seconds=float(payload.get("right_seconds") or 0),
            reason=str(payload.get("reason") or "unspecified"),
        )
    if action == "wait_for_future_evidence":
        return WaitForFutureEvidence(reason=str(payload.get("reason") or "unspecified"))
    raise ValueError(f"unsupported reconciliation action: {action!r}")


class RangeReconcileExecutor:
    """Runs one reconciliation step over an :class:`EvidenceBundle`.

    A range-aware executor exposes ``reconcile_range(bundle, ...) -> ReconcileAction``
    and is used directly. The shipped day executors do not: their prompt and output
    schema are fixed internally, so this adapter runs their ordinary segmentation over
    the range's manifest and reports it as a ``publish``. That is the correct degraded
    behaviour — Chronicle still enforces validation, pinned boundaries, and fencing —
    but such an executor can never ask for expansion or to wait, so a run against it
    always terminates in one iteration.
    """

    def __init__(self, executor: Any):
        self._executor = executor

    async def reconcile(
        self,
        bundle: EvidenceBundle,
        *,
        reasoning_effort: str | None = None,
        validation_feedback: str | None = None,
    ) -> ReconcileAction:
        native = getattr(self._executor, "reconcile_range", None)
        if native is not None:
            return await native(
                bundle,
                reasoning_effort=reasoning_effort,
                validation_feedback=validation_feedback,
            )
        with tempfile.TemporaryDirectory(prefix="chronicle-reconcile-") as temp_dir:
            workspace = Path(temp_dir)
            write_workspace(workspace, bundle.manifest)
            result = await self._executor.analyze(
                workspace,
                bundle.manifest,
                bundle.existing_episodes,
                bundle.pinned_episodes,
                reasoning_effort=reasoning_effort,
                validation_feedback=validation_feedback,
            )
        return Publish(result=result)


def build_range_executor() -> RangeReconcileExecutor:
    return RangeReconcileExecutor(build_executor())
