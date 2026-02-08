"""Model registry and config loader.

Loads a single source of truth from config.yml and exposes model
definitions (LLM, embeddings, etc.) in a provider-agnostic way.

Now using Pydantic for robust validation and type safety.
Environment variable resolution is handled by OmegaConf in the config module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# Import config merging for defaults.yml + config.yml integration
# OmegaConf handles environment variable resolution (${VAR:-default} syntax)
from advanced_omi_backend.config import get_config


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
    model_provider: str = Field(default="unknown", description="Provider name: openai, ollama, deepgram, parakeet, vibevoice, etc.")
    api_family: str = Field(default="openai", description="API family: openai, http, websocket, etc.")
    model_name: str = Field(default="", description="Provider-specific model name")
    model_url: str = Field(default="", description="Base URL for API requests")
    api_key: Optional[str] = Field(default=None, description="API key or authentication token")
    description: Optional[str] = Field(default=None, description="Human-readable description")
    model_params: Dict[str, Any] = Field(default_factory=dict, description="Model-specific parameters")
    model_output: Optional[str] = Field(default=None, description="Output format: json, text, vector, etc.")
    embedding_dimensions: Optional[int] = Field(default=None, ge=1, description="Embedding vector dimensions")
    operations: Dict[str, Any] = Field(default_factory=dict, description="API operation definitions")
    capabilities: List[str] = Field(
        default_factory=list,
        description="Provider capabilities: word_timestamps, segments, diarization (for STT providers)"
    )
    
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
    chat: Dict[str, Any] = Field(
        default_factory=dict,
        description="Chat service configuration including system prompt"
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
    """
    Find config.yml using canonical path from config module.

    DEPRECATED: Use advanced_omi_backend.config.get_config_yml_path() directly.
    Kept for backward compatibility.

    Returns:
        Path to config.yml
    """
    from advanced_omi_backend.config import get_config_yml_path
    return get_config_yml_path()


def load_models_config(force_reload: bool = False) -> Optional[AppModels]:
    """Load model configuration from merged defaults.yml + config.yml.

    This function loads defaults.yml and config.yml, merges them with user overrides,
    validates model definitions using Pydantic, and caches the result.
    Environment variables are resolved by OmegaConf during config loading.

    Args:
        force_reload: If True, reload from disk even if already cached

    Returns:
        AppModels instance with validated configuration, or None if config not found

    Raises:
        ValidationError: If config.yml has invalid model definitions
    """
    global _REGISTRY
    if _REGISTRY is not None and not force_reload:
        return _REGISTRY

    # Get merged configuration (defaults + user config)
    # OmegaConf resolves environment variables automatically
    try:
        raw = get_config(force_reload=force_reload)
    except Exception as e:
        logging.error(f"Failed to load merged configuration: {e}")
        return None

    # Extract sections
    defaults = raw.get("defaults", {}) or {}
    model_list = raw.get("models", []) or []
    memory_settings = raw.get("memory", {}) or {}
    speaker_recognition_cfg = raw.get("speaker_recognition", {}) or {}
    chat_settings = raw.get("chat", {}) or {}

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
        speaker_recognition=speaker_recognition_cfg,
        chat=chat_settings
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
