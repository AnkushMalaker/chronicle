"""
Configuration management for Chronicle backend.

Provides central configuration loading with defaults.yml + config.yml merging.
Also contains diarization and speech detection settings.

Priority: config.yml > environment variables > defaults.yml
"""

import json
import logging
import os
import re
import shutil
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Data directory paths
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CHUNK_DIR = Path("./audio_chunks")  # Mounted to ./data/audio_chunks by Docker

# Default diarization settings
DEFAULT_DIARIZATION_SETTINGS = {
    "diarization_source": "pyannote",
    "similarity_threshold": 0.15,
    "min_duration": 0.5,
    "collar": 2.0,
    "min_duration_off": 1.5,
    "min_speakers": 2,
    "max_speakers": 6
}

# Default speech detection settings
DEFAULT_SPEECH_DETECTION_SETTINGS = {
    "min_words": 10,              # Minimum words to create conversation (increased from 5)
    "min_confidence": 0.7,        # Word confidence threshold (increased from 0.5)
    "min_duration": 10.0,         # Minimum speech duration in seconds (increased from 2.0)
}

# Default conversation stop settings
DEFAULT_CONVERSATION_STOP_SETTINGS = {
    "transcription_buffer_seconds": 120,    # Periodic transcription interval (2 minutes)
    "speech_inactivity_threshold": 60,      # Speech gap threshold for closure (1 minute)
}

# Default audio storage settings
DEFAULT_AUDIO_STORAGE_SETTINGS = {
    "audio_base_path": "/app/data",  # Main audio directory (where volume is mounted)
    "audio_chunks_path": "/app/audio_chunks",  # Full path to audio chunks subfolder
}

# Global cache for diarization settings
_diarization_settings = None


def get_diarization_config_path():
    """Get the path to the diarization config file."""
    # Try different locations in order of preference
    # 1. Data directory (for persistence across container restarts)
    data_path = Path("/app/data/diarization_config.json")
    if data_path.parent.exists():
        return data_path
    
    # 2. App root directory
    app_path = Path("/app/diarization_config.json")
    if app_path.parent.exists():
        return app_path
    
    # 3. Local development path
    local_path = Path("diarization_config.json")
    return local_path


def load_diarization_settings_from_file():
    """Load diarization settings from file or create from template."""
    global _diarization_settings
    
    config_path = get_diarization_config_path()
    template_path = Path("/app/diarization_config.json.template")
    
    # If no template, try local development path
    if not template_path.exists():
        template_path = Path("diarization_config.json.template")
    
    # If config doesn't exist, try to copy from template
    if not config_path.exists():
        if template_path.exists():
            try:
                # Ensure parent directory exists
                config_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(template_path, config_path)
                logger.info(f"Created diarization config from template at {config_path}")
            except Exception as e:
                logger.warning(f"Could not copy template to {config_path}: {e}")
    
    # Load from file if it exists
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                _diarization_settings = json.load(f)
                logger.info(f"Loaded diarization settings from {config_path}")
                return _diarization_settings
        except Exception as e:
            logger.error(f"Error loading diarization settings from {config_path}: {e}")
    
    # Fall back to defaults
    _diarization_settings = DEFAULT_DIARIZATION_SETTINGS.copy()
    logger.info("Using default diarization settings")
    return _diarization_settings


def save_diarization_settings_to_file(settings):
    """Save diarization settings to file."""
    global _diarization_settings
    
    config_path = get_diarization_config_path()
    
    try:
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write settings to file
        with open(config_path, 'w') as f:
            json.dump(settings, f, indent=2)
        
        # Update cache
        _diarization_settings = settings
        
        logger.info(f"Saved diarization settings to {config_path}")
        return True
    except Exception as e:
        logger.error(f"Error saving diarization settings to {config_path}: {e}")
        return False


def get_speech_detection_settings():
    """Get speech detection settings from environment or defaults."""

    return {
        "min_words": int(os.getenv("SPEECH_DETECTION_MIN_WORDS", DEFAULT_SPEECH_DETECTION_SETTINGS["min_words"])),
        "min_confidence": float(os.getenv("SPEECH_DETECTION_MIN_CONFIDENCE", DEFAULT_SPEECH_DETECTION_SETTINGS["min_confidence"])),
        "min_duration": float(os.getenv("SPEECH_DETECTION_MIN_DURATION", DEFAULT_SPEECH_DETECTION_SETTINGS["min_duration"])),
    }


def get_conversation_stop_settings():
    """Get conversation stop settings from environment or defaults."""

    return {
        "transcription_buffer_seconds": float(os.getenv("TRANSCRIPTION_BUFFER_SECONDS", DEFAULT_CONVERSATION_STOP_SETTINGS["transcription_buffer_seconds"])),
        "speech_inactivity_threshold": float(os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", DEFAULT_CONVERSATION_STOP_SETTINGS["speech_inactivity_threshold"])),
        "min_word_confidence": float(os.getenv("SPEECH_DETECTION_MIN_CONFIDENCE", DEFAULT_SPEECH_DETECTION_SETTINGS["min_confidence"])),
    }


def get_audio_storage_settings():
    """Get audio storage settings from environment or defaults."""
    
    # Get base path and derive chunks path
    audio_base_path = os.getenv("AUDIO_BASE_PATH", DEFAULT_AUDIO_STORAGE_SETTINGS["audio_base_path"])
    audio_chunks_path = os.getenv("AUDIO_CHUNKS_PATH", f"{audio_base_path}/audio_chunks")
    
    return {
        "audio_base_path": audio_base_path,
        "audio_chunks_path": audio_chunks_path,
    }


# Initialize settings on module load
_diarization_settings = load_diarization_settings_from_file()


# ==============================================================================
# General Configuration Loading (config.yml + defaults.yml)
# ==============================================================================

# Cache for merged configuration
_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def _resolve_env(value: Any) -> Any:
    """Resolve ``${VAR:-default}`` patterns inside a single value.

    This helper is intentionally minimal: it only operates on strings and leaves
    all other types unchanged. Patterns of the form ``${VAR}`` or
    ``${VAR:-default}`` are expanded using ``os.getenv``:

    - If the environment variable **VAR** is set, its value is used.
    - Otherwise the optional ``default`` is used (or ``""`` if omitted).

    Examples:
        >>> os.environ.get("OLLAMA_MODEL")
        >>> _resolve_env("${OLLAMA_MODEL:-llama3.1:latest}")
        'llama3.1:latest'

        >>> os.environ["OLLAMA_MODEL"] = "llama3.2:latest"
        >>> _resolve_env("${OLLAMA_MODEL:-llama3.1:latest}")
        'llama3.2:latest'

        >>> _resolve_env("Bearer ${OPENAI_API_KEY:-}")
        'Bearer '  # when OPENAI_API_KEY is not set
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
    """
    if isinstance(data, dict):
        return {k: _deep_resolve_env(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_resolve_env(v) for v in data]
    return _resolve_env(data)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries, with override taking precedence.

    Args:
        base: Base dictionary (defaults)
        override: Override dictionary (from config.yml)

    Returns:
        Merged dictionary
    """
    result = base.copy()
    try:
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = value
    except ValueError as e:
        logger.error(f"Error in _deep_merge: {e}, base type: {type(base)}, override type: {type(override)}")
        raise
    return result


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


def get_config(force_reload: bool = False) -> Dict[str, Any]:
    """Get the full merged configuration (defaults.yml + config.yml + env vars).

    This is the central function for loading configuration. It merges:
    1. defaults.yml (fallback defaults)
    2. config.yml (user overrides)
    3. Environment variable resolution (${VAR:-default})

    Priority: config.yml > environment variables > defaults.yml

    Args:
        force_reload: If True, reload from disk even if already cached

    Returns:
        Complete merged configuration dictionary with all sections

    Example:
        >>> config = get_config()
        >>> memory_config = config.get("memory", {})
        >>> chat_config = config.get("chat", {})
        >>> models = config.get("models", [])
    """
    global _CONFIG_CACHE

    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    # Find config.yml path
    cfg_path = _find_config_path()

    # Load defaults.yml from same directory as config.yml
    defaults_path = cfg_path.parent / "defaults.yml"
    if defaults_path.exists():
        try:
            with defaults_path.open("r") as f:
                raw = yaml.safe_load(f) or {}
            logger.info(f"Loaded defaults from {defaults_path}")
        except Exception as e:
            logger.error(f"Failed to load defaults from {defaults_path}: {e}")
            raw = {}
    else:
        logger.warning(f"No defaults.yml found at {defaults_path}, starting with empty config")
        raw = {}

    # Try to load config.yml and merge with defaults
    if cfg_path.exists():
        try:
            with cfg_path.open("r") as f:
                user_config = yaml.safe_load(f) or {}

            # Merge user config over defaults (config.yml takes precedence)
            raw = _deep_merge(raw, user_config)
            logger.info(f"Loaded config from {cfg_path} (merged with defaults)")
        except Exception as e:
            logger.warning(f"Failed to load {cfg_path}, using defaults only: {e}")
    else:
        logger.info(f"No config.yml found at {cfg_path}, using defaults only")

    # Resolve environment variables
    raw = _deep_resolve_env(raw)

    # Cache the result
    _CONFIG_CACHE = raw

    return raw


def reload_config() -> Dict[str, Any]:
    """Force reload configuration from disk.

    This is useful after configuration files have been modified.

    Returns:
        Complete merged configuration dictionary
    """
    return get_config(force_reload=True)


def get_config_section(section: str, default: Any = None) -> Any:
    """Get a specific section from the merged configuration.

    Args:
        section: Section name (e.g., "memory", "chat", "models")
        default: Default value if section doesn't exist

    Returns:
        Configuration section or default value

    Example:
        >>> memory_config = get_config_section("memory", {})
        >>> models = get_config_section("models", [])
    """
    config = get_config()
    return config.get(section, default)


def get_config_path() -> Path:
    """Get the path to config.yml being used.

    Returns:
        Path to config.yml
    """
    return _find_config_path()