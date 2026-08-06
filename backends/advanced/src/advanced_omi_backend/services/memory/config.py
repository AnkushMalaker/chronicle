"""Memory service configuration utilities."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import yaml

from advanced_omi_backend.config import get_config_yml_path
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
    # Agent backends are independent for write and search.  The write recovery
    # backend is invoked only when the primary backend fails to create a valid
    # conversation note; ``None`` skips agent recovery and uses the deterministic
    # source-preserving note fallback immediately.
    write_agent_backend: str = "direct"
    write_recovery_backend: Optional[str] = "direct"
    search_agent_backend: str = "direct"


def load_config_yml() -> Dict[str, Any]:
    """
    Load config.yml using canonical path from config module.

    Returns:
        Loaded config.yml as dictionary

    Raises:
        FileNotFoundError: If config.yml does not exist
    """
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
        legacy_memory_keys = sorted(
            {"agent_executor", "codex", "pi"}.intersection(mem_settings)
        )
        if legacy_memory_keys:
            raise ValueError(
                "Obsolete flat memory configuration found: "
                + ", ".join(f"memory.{key}" for key in legacy_memory_keys)
                + ". Run ./wizard.sh to replace it with memory.agents and "
                "memory.backends; Chronicle will not guess a new executor."
            )
        if "memory_agent" in getattr(reg, "llm_operations", {}):
            raise ValueError(
                "Obsolete llm_operations.memory_agent configuration found. Run "
                "./wizard.sh to replace the memory-agent schema with memory_write "
                "and memory_search; Chronicle will not silently change its model."
            )
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
        extraction_cfg = mem_settings.get("extraction")
        if extraction_cfg is None:
            extraction_cfg = {}
        if not isinstance(extraction_cfg, dict):
            raise ValueError("memory.extraction must be a mapping")
        extraction_enabled = bool(extraction_cfg.get("enabled", True))
        extraction_prompt = extraction_cfg.get("prompt") if extraction_enabled else None

        # Timeouts/tunables from registry.memory
        timeout_seconds = int(mem_settings.get("timeout_seconds", 1200))
        agents_cfg = mem_settings.get("agents")
        if agents_cfg is None:
            agents_cfg = {}
        if not isinstance(agents_cfg, dict):
            raise ValueError("memory.agents must be a mapping")
        write_cfg = agents_cfg.get("write")
        search_cfg = agents_cfg.get("search")
        if write_cfg is None:
            write_cfg = {}
        if search_cfg is None:
            search_cfg = {}
        if not isinstance(write_cfg, dict) or not isinstance(search_cfg, dict):
            raise ValueError("memory.agents.write/search must be mappings")

        write_agent_backend = str(write_cfg.get("backend") or "direct").lower()
        raw_recovery = write_cfg.get("recovery_backend", "direct")
        write_recovery_backend = (
            str(raw_recovery).lower() if raw_recovery not in (None, "") else None
        )
        search_agent_backend = str(search_cfg.get("backend") or "direct").lower()

        write_backends = {"direct", "codex", "pi"}
        search_backends = {"direct", "pi"}
        if write_agent_backend not in write_backends:
            raise ValueError(f"Unsupported memory write backend: {write_agent_backend}")
        if (
            write_recovery_backend is not None
            and write_recovery_backend not in write_backends
        ):
            raise ValueError(
                f"Unsupported memory write recovery backend: {write_recovery_backend}"
            )
        if search_agent_backend not in search_backends:
            raise ValueError(
                f"Unsupported memory search backend: {search_agent_backend}"
            )

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
            write_agent_backend=write_agent_backend,
            write_recovery_backend=write_recovery_backend,
            search_agent_backend=search_agent_backend,
        )

    except ImportError:
        memory_logger.warning(
            "Config loader not available, using environment variables only"
        )
        raise
