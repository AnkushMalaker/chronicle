"""Swappable VAD providers.

The active provider is selected by ``data_audit.vad_provider`` in config
(default ``ten_vad``). ``get_vad_provider()`` returns a fresh instance per
call because providers may carry streaming state — use one instance per
audio stream and feed it chunks in order.
"""

from typing import Callable, Dict

from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.services.vad.base import VADProvider
from advanced_omi_backend.services.vad.ten_vad import TenVadProvider

DEFAULT_PROVIDER = "ten_vad"


def _make_ten_vad() -> VADProvider:
    return TenVadProvider()


# Provider factories are lazy so importing this package never loads native libs.
_REGISTRY: Dict[str, Callable[[], VADProvider]] = {
    "ten_vad": _make_ten_vad,
}


def get_vad_provider(name: str | None = None) -> VADProvider:
    """Build the configured VAD provider (or the named one)."""
    if name is None:
        cfg = get_service_config("data_audit")
        name = cfg.get("vad_provider", DEFAULT_PROVIDER)
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown VAD provider '{name}'; available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()
