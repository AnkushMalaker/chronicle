"""Configuration for manual-memory media enrichment."""

from dataclasses import dataclass
from typing import Any

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import load_config
from advanced_omi_backend.services.vision import codex_vision_settings

MAX_IMAGE_ANALYSIS_ATTEMPTS = 3


@dataclass(frozen=True)
class ManualMemorySettings:
    codex: dict[str, Any]

    @property
    def timeout_seconds(self) -> int:
        return int(self.codex["timeout_seconds"])


def manual_memory_settings(settings: Any = None) -> ManualMemorySettings:
    if settings is None:
        value = load_config().get("manual_memories", {})
        settings = (
            OmegaConf.to_container(value, resolve=True)
            if OmegaConf.is_config(value)
            else value
        )
    if not isinstance(settings, dict):
        raise ValueError("manual_memories must be a mapping")
    agents = settings.get("agents") or {}
    analyze = agents.get("analyze_image") or {}
    if analyze.get("backend", "codex") != "codex":
        raise ValueError("manual_memories.agents.analyze_image.backend must be codex")
    backends = settings.get("backends") or {}
    codex = codex_vision_settings(
        backends.get("codex") or {}, label="manual_memories.backends.codex"
    )
    return ManualMemorySettings(codex=codex)
