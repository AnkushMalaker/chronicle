"""Configuration for manual-memory media enrichment."""

from dataclasses import dataclass
from typing import Any

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import load_config
from advanced_omi_backend.services.vision import structured_vision_settings

MAX_IMAGE_ANALYSIS_ATTEMPTS = 3


@dataclass(frozen=True)
class ManualMemorySettings:
    vision: dict[str, Any]

    @property
    def timeout_seconds(self) -> int:
        return int(self.vision["timeout_seconds"])


def manual_memory_settings(settings: Any = None) -> ManualMemorySettings:
    codex_settings: dict[str, Any] = {}
    if settings is None:
        config = load_config()
        value = config.get("manual_memories", {})
        codex_value = ((config.get("vision") or {}).get("backends") or {}).get(
            "codex", {}
        )
        codex_settings = (
            OmegaConf.to_container(codex_value, resolve=True)
            if OmegaConf.is_config(codex_value)
            else codex_value
        )
        settings = (
            OmegaConf.to_container(value, resolve=True)
            if OmegaConf.is_config(value)
            else value
        )
    if not isinstance(settings, dict):
        raise ValueError("manual_memories must be a mapping")
    agents = settings.get("agents") or {}
    analyze = agents.get("analyze_image") or {}
    vision = structured_vision_settings(
        analyze,
        label="manual_memories.agents.analyze_image",
        default_operation="manual_memory_image",
        codex_settings=codex_settings,
    )
    return ManualMemorySettings(vision=vision)
