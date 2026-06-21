"""Memory service configuration utilities."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import yaml

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.utils.config_utils import resolve_value

memory_logger = logging.getLogger("memory_service")

__all__ = ["resolve_value"]


class LLMProvider(Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"
    CUSTOM = "custom"


class MemoryProvider(Enum):
    """Supported memory service providers."""

    CHRONICLE = "chronicle"  # Agentic Markdown vault (the only provider)


@dataclass
class MemoryConfig:
    """Configuration for memory service."""

    memory_provider: MemoryProvider = MemoryProvider.CHRONICLE
    llm_provider: LLMProvider = LLMProvider.OPENAI
    llm_config: Dict[str, Any] = None
    embedder_config: Dict[str, Any] = None
    extraction_prompt: str = None
    extraction_enabled: bool = True
    timeout_seconds: int = 1200


def load_config_yml() -> Dict[str, Any]:
    """
    Load config.yml using canonical path from config module.

    Returns:
        Loaded config.yml as dictionary

    Raises:
        FileNotFoundError: If config.yml does not exist
    """
    from advanced_omi_backend.config import get_config_yml_path

    config_path = get_config_yml_path()

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yml not found at {config_path}. "
            "Ensure config directory is mounted correctly."
        )

    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def create_openai_config(
    api_key: str,
    model: str,
    *,
    embedding_model: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.1,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """Create OpenAI/OpenAI-compatible client configuration."""
    return {
        "api_key": api_key,
        "model": model,
        "embedding_model": embedding_model or model,
        "base_url": base_url,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def build_memory_config_from_env() -> MemoryConfig:
    """Build memory configuration from environment variables and YAML config."""
    try:
        # Determine memory provider from registry
        reg = get_models_registry()
        mem_settings = reg.memory if reg else {}
        memory_provider = (mem_settings.get("provider") or "chronicle").lower()

        # Map legacy provider names to current names
        if memory_provider in ("friend-lite", "friend_lite"):
            memory_logger.info(
                f"🔧 Mapping legacy provider '{memory_provider}' to 'chronicle'"
            )
            memory_provider = "chronicle"

        if memory_provider not in [p.value for p in MemoryProvider]:
            raise ValueError(f"Unsupported memory provider: {memory_provider}")

        memory_provider_enum = MemoryProvider(memory_provider)

        # Chronicle uses registry-driven OpenAI-compatible LLM configuration
        # (the memory agent calls the LLM via the operations registry).
        llm_config = None
        llm_provider_enum = LLMProvider.OPENAI  # OpenAI-compatible API family
        if not reg:
            raise ValueError("config.yml not found; cannot configure LLM provider")
        llm_def = reg.get_default("llm")
        embed_def = reg.get_default("embedding")
        if not llm_def:
            raise ValueError("No default LLM defined in config.yml")
        model = llm_def.model_name
        embedding_model = (
            embed_def.model_name if embed_def else "text-embedding-3-small"
        )
        base_url = llm_def.resolved_url()
        memory_logger.info(
            f"🔧 Memory config (registry): LLM={model}, Embedding={embedding_model}, Base URL={base_url}"
        )
        llm_config = create_openai_config(
            api_key=llm_def.api_key or "",
            model=model,
            embedding_model=embedding_model,
            base_url=base_url,
            temperature=float(llm_def.model_params.get("temperature", 0.1)),
            max_tokens=int(llm_def.model_params.get("max_tokens", 2000)),
        )

        # Get memory extraction settings from registry
        extraction_cfg = mem_settings.get("extraction") or {}
        extraction_enabled = bool(extraction_cfg.get("enabled", True))
        extraction_prompt = extraction_cfg.get("prompt") if extraction_enabled else None

        # Timeouts/tunables from registry.memory
        timeout_seconds = int(mem_settings.get("timeout_seconds", 1200))

        memory_logger.info(
            f"🔧 Memory config: Provider={memory_provider_enum.value}, "
            f"LLM={llm_def.model_provider}, Extraction={extraction_enabled}"
        )

        return MemoryConfig(
            memory_provider=memory_provider_enum,
            llm_provider=llm_provider_enum,
            llm_config=llm_config,
            embedder_config={},  # Included in llm_config
            extraction_prompt=extraction_prompt,
            extraction_enabled=extraction_enabled,
            timeout_seconds=timeout_seconds,
        )

    except ImportError:
        memory_logger.warning(
            "Config loader not available, using environment variables only"
        )
        raise
