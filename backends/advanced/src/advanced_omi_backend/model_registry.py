"""Model registry and config loader.

Loads a single source of truth from config.yml and exposes model
definitions (LLM, embeddings, etc.) in a provider-agnostic way.

Now using Pydantic for robust validation and type safety.
"""

from __future__ import annotations

import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict, ValidationError

def _resolve_env(value: Any) -> Any:
    """Resolve ``${VAR:-default}`` patterns inside a single value.
    
    This helper is intentionally minimal: it only operates on strings and leaves
    all other types unchanged. Patterns of the form ``${VAR}`` or
    ``${VAR:-default}`` are expanded using ``os.getenv``:
    
    - If the environment variable **VAR** is set, its value is used.
    - Otherwise the optional ``default`` is used (or ``\"\"`` if omitted).
    
    Examples:
        >>> os.environ.get("OLLAMA_MODEL")
        >>> _resolve_env("${OLLAMA_MODEL:-llama3.1:latest}")
        'llama3.1:latest'
        
        >>> os.environ["OLLAMA_MODEL"] = "llama3.2:latest"
        >>> _resolve_env("${OLLAMA_MODEL:-llama3.1:latest}")
        'llama3.2:latest'
        
        >>> _resolve_env("Bearer ${OPENAI_API_KEY:-}")
        'Bearer '  # when OPENAI_API_KEY is not set
    
    Note:
        Use :func:`_deep_resolve_env` to apply this logic to an entire
        nested config structure (dicts/lists) loaded from YAML.
    """
    if not isinstance(value, str):
        return value

    pattern = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")

    def repl(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        return os.getenv(var, default or "")

    return pattern.sub(repl, value)


def _deep_resolve_env(data: Any) -> Any:
    """Recursively resolve environment variables in nested structures.
    
    This walks arbitrary Python structures produced by ``yaml.safe_load`` and
    applies :func:`_resolve_env` to every string it finds. Dictionaries and
    lists are traversed deeply; scalars are passed through unchanged.
    
    Examples:
        >>> os.environ["OPENAI_MODEL"] = "gpt-4o-mini"
        >>> cfg = {
        ...     "models": [
        ...         {"model_name": "${OPENAI_MODEL:-gpt-4o-mini}"},
        ...         {"model_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}"}
        ...     ]
        ... }
        >>> resolved = _deep_resolve_env(cfg)
        >>> resolved["models"][0]["model_name"]
        'gpt-4o-mini'
        >>> resolved["models"][1]["model_url"]
        'https://api.openai.com/v1'
    
    This is what :func:`load_models_config` uses immediately after loading
    ``config.yml`` so that all ``${VAR:-default}`` placeholders are resolved
    before Pydantic validation and model registry construction.
    """
    if isinstance(data, dict):
        return {k: _deep_resolve_env(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_resolve_env(v) for v in data]
    return _resolve_env(data)


class ModelDef(BaseModel):
    """Model definition with validation.
    
    Represents a single model configuration (LLM, embedding, STT, TTS, etc.)
    from config.yml with automatic validation and type checking.
    """
    
    model_config = ConfigDict(
        extra='allow',  # Allow extra fields for extensibility
        validate_assignment=True,  # Validate on attribute assignment
        arbitrary_types_allowed=True,
    )
    
    name: str = Field(..., min_length=1, description="Unique model identifier")
    model_type: str = Field(..., description="Model type: llm, embedding, stt, tts, etc.")
    model_provider: str = Field(default="unknown", description="Provider name: openai, ollama, deepgram, parakeet, etc.")
    api_family: str = Field(default="openai", description="API family: openai, http, websocket, etc.")
    model_name: str = Field(default="", description="Provider-specific model name")
    model_url: str = Field(default="", description="Base URL for API requests")
    api_key: Optional[str] = Field(default=None, description="API key or authentication token")
    description: Optional[str] = Field(default=None, description="Human-readable description")
    model_params: Dict[str, Any] = Field(default_factory=dict, description="Model-specific parameters")
    model_output: Optional[str] = Field(default=None, description="Output format: json, text, vector, etc.")
    embedding_dimensions: Optional[int] = Field(default=None, ge=1, description="Embedding vector dimensions")
    operations: Dict[str, Any] = Field(default_factory=dict, description="API operation definitions")
    
    @field_validator('model_name', mode='before')
    @classmethod
    def default_model_name(cls, v: Any, info) -> str:
        """Default model_name to name if not provided."""
        if not v and info.data.get('name'):
            return info.data['name']
        return v or ""
    
    @field_validator('model_url', mode='before')
    @classmethod
    def validate_url(cls, v: Any) -> str:
        """Ensure URL doesn't have trailing whitespace."""
        if isinstance(v, str):
            return v.strip()
        return v or ""
    
    @field_validator('api_key', mode='before')
    @classmethod
    def sanitize_api_key(cls, v: Any) -> Optional[str]:
        """Sanitize API key, treat empty strings as None."""
        if isinstance(v, str):
            v = v.strip()
            if not v or v.lower() in ['dummy', 'none', 'null']:
                return None
            return v
        return v
    
    @model_validator(mode='after')
    def validate_model(self) -> ModelDef:
        """Cross-field validation."""
        # Ensure embedding models have dimensions specified
        if self.model_type == 'embedding' and not self.embedding_dimensions:
            # Common defaults
            defaults = {
                'text-embedding-3-small': 1536,
                'text-embedding-3-large': 3072,
                'text-embedding-ada-002': 1536,
                'nomic-embed-text-v1.5': 768,
            }
            if self.model_name in defaults:
                self.embedding_dimensions = defaults[self.model_name]
        
        return self


class AppModels(BaseModel):
    """Application models registry.
    
    Contains default model selections and all available model definitions.
    """
    
    model_config = ConfigDict(
        extra='allow',
        validate_assignment=True,
    )
    
    defaults: Dict[str, str] = Field(
        default_factory=dict,
        description="Default model names for each model_type"
    )
    models: Dict[str, ModelDef] = Field(
        default_factory=dict,
        description="All available model definitions keyed by name"
    )
    memory: Dict[str, Any] = Field(
        default_factory=dict,
        description="Memory service configuration"
    )
    speaker_recognition: Dict[str, Any] = Field(
        default_factory=dict,
        description="Speaker recognition service configuration"
    )
    
    def get_by_name(self, name: str) -> Optional[ModelDef]:
        """Get a model by its unique name.
        
        Args:
            name: Model name to look up
            
        Returns:
            ModelDef if found, None otherwise
        """
        return self.models.get(name)
    
    def get_default(self, model_type: str) -> Optional[ModelDef]:
        """Get the default model for a given type.
        
        Args:
            model_type: Type of model (llm, embedding, stt, tts, etc.)
            
        Returns:
            Default ModelDef for the type, or first available model of that type,
            or None if no models of that type exist
        """
        # Try explicit default first
        name = self.defaults.get(model_type)
        if name:
            model = self.get_by_name(name)
            if model:
                return model
        
        # Fallback: first model of that type
        for m in self.models.values():
            if m.model_type == model_type:
                return m
        
        return None
    
    def get_all_by_type(self, model_type: str) -> List[ModelDef]:
        """Get all models of a specific type.
        
        Args:
            model_type: Type of model to filter by
            
        Returns:
            List of ModelDef objects matching the type
        """
        return [m for m in self.models.values() if m.model_type == model_type]
    
    def list_model_types(self) -> List[str]:
        """Get all unique model types in the registry.
        
        Returns:
            Sorted list of model types
        """
        return sorted(set(m.model_type for m in self.models.values()))


# Global registry singleton
_REGISTRY: Optional[AppModels] = None


def _find_config_path() -> Path:
    """Find config.yml in expected locations.
    
    Search order:
    1. CONFIG_FILE environment variable
    2. Current working directory
    3. /app/config.yml (Docker container)
    4. Walk up from module directory
    
    Returns:
        Path to config.yml (may not exist)
    """
    # ENV override
    cfg_env = os.getenv("CONFIG_FILE")
    if cfg_env and Path(cfg_env).exists():
        return Path(cfg_env)

    # Common locations (container vs repo root)
    candidates = [Path("config.yml"), Path("/app/config.yml")]

    # Also walk up from current file's parents defensively
    try:
        for parent in Path(__file__).resolve().parents:
            c = parent / "config.yml"
            if c.exists():
                return c
    except Exception:
        pass

    for c in candidates:
        if c.exists():
            return c
    
    # Last resort: return /app/config.yml path (may not exist yet)
    return Path("/app/config.yml")


def load_models_config(force_reload: bool = False) -> Optional[AppModels]:
    """Load model configuration from config.yml.
    
    This function loads and parses the config.yml file, resolves environment
    variables, validates model definitions using Pydantic, and caches the result.
    
    Args:
        force_reload: If True, reload from disk even if already cached
        
    Returns:
        AppModels instance with validated configuration, or None if config not found
        
    Raises:
        ValidationError: If config.yml has invalid model definitions
        yaml.YAMLError: If config.yml has invalid YAML syntax
    """
    global _REGISTRY
    if _REGISTRY is not None and not force_reload:
        return _REGISTRY

    cfg_path = _find_config_path()
    if not cfg_path.exists():
        return None

    # Load and parse YAML
    with cfg_path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    
    # Resolve environment variables
    raw = _deep_resolve_env(raw)

    # Extract sections
    defaults = raw.get("defaults", {}) or {}
    model_list = raw.get("models", []) or []
    memory_settings = raw.get("memory", {}) or {}
    speaker_recognition_cfg = raw.get("speaker_recognition", {}) or {}

    # Parse and validate models using Pydantic
    models: Dict[str, ModelDef] = {}
    for m in model_list:
        try:
            # Pydantic will handle validation automatically
            model_def = ModelDef(**m)
            models[model_def.name] = model_def
        except ValidationError as e:
            # Log but don't fail the entire registry load
            logging.warning(f"Failed to load model '{m.get('name', 'unknown')}': {e}")
            continue

    # Create and cache registry
    _REGISTRY = AppModels(
        defaults=defaults,
        models=models,
        memory=memory_settings,
        speaker_recognition=speaker_recognition_cfg
    )
    return _REGISTRY


def get_models_registry() -> Optional[AppModels]:
    """Get the global models registry.
    
    This is the primary interface for accessing model configurations.
    The registry is loaded once and cached for performance.
    
    Returns:
        AppModels instance, or None if config.yml not found
        
    Example:
        >>> registry = get_models_registry()
        >>> if registry:
        ...     llm = registry.get_default('llm')
        ...     print(f"Default LLM: {llm.name} ({llm.model_provider})")
    """
    return load_models_config(force_reload=False)
