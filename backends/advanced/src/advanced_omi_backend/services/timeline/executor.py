"""Validation and executor selection for semantic timeline analysis."""

from typing import Any

from .contracts import TimelineAgentResult, TimelineEvidenceManifest


def validate_agent_result(
    result: TimelineAgentResult, manifest: TimelineEvidenceManifest
) -> None:
    if manifest.evidence and not result.episodes and not result.unassigned_intervals:
        raise ValueError("agent result accounts for no evidence intervals")

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
