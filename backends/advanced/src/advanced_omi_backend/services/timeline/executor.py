"""Validation and executor selection for semantic timeline analysis."""

from datetime import datetime, timedelta
from typing import Any

from .contracts import TimelineAgentResult, TimelineEvidenceManifest

BOUNDARY_SUPPORT_TOLERANCE = timedelta(minutes=2)
ACCOUNTING_GAP_TOLERANCE = timedelta(minutes=1)


def _evidence_end(item) -> datetime:
    return item.ended_at or item.started_at


def _supports_boundary(item, boundary: datetime) -> bool:
    return (
        item.started_at - BOUNDARY_SUPPORT_TOLERANCE
        <= boundary
        <= _evidence_end(item) + BOUNDARY_SUPPORT_TOLERANCE
    )


def _validate_evidence_accounting(
    result: TimelineAgentResult, manifest: TimelineEvidenceManifest
) -> None:
    accounted = sorted(
        [
            (interval.started_at, interval.ended_at)
            for interval in result.unassigned_intervals
        ]
        + [(episode.started_at, episode.ended_at) for episode in result.episodes],
        key=lambda interval: interval[0],
    )
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


def validate_agent_result(
    result: TimelineAgentResult, manifest: TimelineEvidenceManifest
) -> None:
    if manifest.evidence and not result.episodes and not result.unassigned_intervals:
        raise ValueError("agent result accounts for no evidence intervals")

    for index, interval in enumerate(result.unassigned_intervals):
        if interval.ended_at <= interval.started_at:
            raise ValueError(f"unassigned interval {index} must have positive duration")
        if (
            interval.started_at < manifest.started_at
            or interval.ended_at > manifest.ended_at
        ):
            raise ValueError(
                f"unassigned interval {index} lies outside the analyzed range"
            )

    evidence = {item.evidence_id: item for item in manifest.evidence}
    for index, episode in enumerate(result.episodes):
        if (
            episode.started_at < manifest.started_at
            or episode.ended_at > manifest.ended_at
        ):
            raise ValueError(f"episode {index} lies outside the analyzed day range")
        unknown = set(episode.evidence_ids) - evidence.keys()
        if unknown:
            raise ValueError(
                f"episode {index} references unknown evidence: {sorted(unknown)}"
            )
        overlapping_evidence_ids: list[str] = []
        for evidence_id in episode.evidence_ids:
            item = evidence[evidence_id]
            item_end = item.ended_at or item.started_at
            if item.started_at < episode.ended_at and item_end > episode.started_at:
                overlapping_evidence_ids.append(evidence_id)
        if not overlapping_evidence_ids:
            raise ValueError(f"episode {index} has no temporally overlapping evidence")
        episode.evidence_ids = overlapping_evidence_ids
        cited_evidence = [evidence[evidence_id] for evidence_id in episode.evidence_ids]
        if not any(
            _supports_boundary(item, episode.started_at) for item in cited_evidence
        ):
            raise ValueError(
                f"episode {index} has no evidence supporting its starting boundary"
            )
        if not any(
            _supports_boundary(item, episode.ended_at) for item in cited_evidence
        ):
            raise ValueError(
                f"episode {index} has no evidence supporting its ending boundary"
            )
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
        if episode.parent_episode_index is not None:
            if episode.parent_episode_index >= len(result.episodes):
                raise ValueError(f"episode {index} parent index is out of range")
            if episode.parent_episode_index == index:
                raise ValueError(f"episode {index} cannot parent itself")

    _validate_evidence_accounting(result, manifest)


def settings_dict() -> dict[str, Any]:
    from omegaconf import OmegaConf

    from advanced_omi_backend.config_loader import load_config

    value = load_config().get("timeline", {})
    converted = OmegaConf.to_container(value, resolve=True)
    return converted if isinstance(converted, dict) else {}


def build_executor():
    settings = settings_dict()
    executor = str(settings.get("executor") or "codex")
    if executor != "codex":
        raise ValueError(f"unsupported timeline executor: {executor}")
    from .codex_executor import CodexTimelineExecutor

    return CodexTimelineExecutor(settings.get("codex") or {})
