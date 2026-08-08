"""Configuration for the screenshot description pass.

Shaped like ``memory.agents``/``memory.backends`` so the executor is selectable the
same way the memory agents are. Only ``codex`` is implemented; ``pi`` is a declared
slot that fails loudly rather than silently falling back, because a silent fallback
would change which model reads the user's images without anything saying so.
"""

from dataclasses import dataclass
from typing import Any

from omegaconf import OmegaConf

from advanced_omi_backend.config_loader import load_config
from advanced_omi_backend.services.vision import codex_vision_settings

DESCRIBE_BACKENDS = {"codex", "pi"}
IMPLEMENTED_DESCRIBE_BACKENDS = {"codex"}
# Attempts before an item is left terminally undescribed. A screenshot that fails
# three separate Codex runs is not going to succeed on the fourth.
MAX_DESCRIBE_ATTEMPTS = 3


@dataclass(frozen=True)
class ScreenshotSettings:
    describe_backend: str
    codex: dict[str, Any]

    @property
    def timeout_seconds(self) -> int:
        return int(self.codex["timeout_seconds"])


def settings_dict() -> dict[str, Any]:
    value = load_config().get("screenshots", {})
    if OmegaConf.is_config(value):
        return OmegaConf.to_container(value, resolve=True)
    return dict(value or {})


def screenshot_settings(settings: Any = None) -> ScreenshotSettings:
    """Validate the ``screenshots`` config block."""

    if settings is None:
        settings = settings_dict()
    if not isinstance(settings, dict):
        raise ValueError("screenshots must be a mapping")

    agents = settings.get("agents") or {}
    describe = agents.get("describe") or {}
    if not isinstance(agents, dict) or not isinstance(describe, dict):
        raise ValueError("screenshots.agents.describe must be a mapping")
    backend = str(describe.get("backend") or "codex").strip().lower()
    if backend not in DESCRIBE_BACKENDS:
        allowed = ", ".join(sorted(DESCRIBE_BACKENDS))
        raise ValueError(
            f"Unsupported screenshot describe backend: {backend} (expected {allowed})"
        )
    if backend not in IMPLEMENTED_DESCRIBE_BACKENDS:
        raise ValueError(
            f"The {backend} screenshot describer is not implemented; "
            "set screenshots.agents.describe.backend: codex"
        )

    backends = settings.get("backends") or {}
    if not isinstance(backends, dict):
        raise ValueError("screenshots.backends must be a mapping")
    codex = codex_vision_settings(
        backends.get("codex") or {}, label="screenshots.backends.codex"
    )
    return ScreenshotSettings(describe_backend=backend, codex=codex)
