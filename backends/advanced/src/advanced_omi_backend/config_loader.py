"""
OmegaConf-based configuration management for Chronicle.

Provides unified config loading with environment variable interpolation.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from omegaconf import DictConfig, OmegaConf

logger = logging.getLogger(__name__)

# Global config cache
_config_cache: Optional[DictConfig] = None


def get_config_dir() -> Path:
    """Get config directory path (single source of truth)."""
    config_dir = os.getenv("CONFIG_DIR", "/app/config")
    return Path(config_dir)


def get_plugins_yml_path() -> Path:
    """
    Get path to plugins.yml file (single source of truth).

    Returns:
        Path to plugins.yml
    """
    return get_config_dir() / "plugins.yml"


def load_config(force_reload: bool = False) -> DictConfig:
    """
    Load and merge configuration using OmegaConf.

    Merge priority (later overrides earlier):
    1. config/defaults.yml (shipped defaults)
    2. config/config.yml (user overrides)
    3. Environment variables (via ${oc.env:VAR,default} syntax)

    Args:
        force_reload: If True, reload from disk even if cached

    Returns:
        Merged DictConfig with all settings
    """
    global _config_cache

    if _config_cache is not None and not force_reload:
        return _config_cache

    config_dir = get_config_dir()
    defaults_path = config_dir / "defaults.yml"

    # Support CONFIG_FILE env var for test configurations
    config_file = os.getenv("CONFIG_FILE", "config.yml")
    # Handle both absolute paths and relative filenames
    if os.path.isabs(config_file):
        config_path = Path(config_file)
    else:
        config_path = config_dir / config_file

    # Load defaults
    defaults = {}
    if defaults_path.exists():
        try:
            defaults = OmegaConf.load(defaults_path)
            logger.info(f"Loaded defaults from {defaults_path}")
        except Exception as e:
            logger.warning(f"Could not load defaults from {defaults_path}: {e}")

    # Load user config
    user_config = {}
    if config_path.exists():
        try:
            user_config = OmegaConf.load(config_path)
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.error(f"Error loading config from {config_path}: {e}")

    # Merge configurations (user config overrides defaults)
    # OmegaConf.merge replaces lists entirely, so we need custom merge
    # for the 'models' list: merge by name so defaults models that aren't
    # in user config are still available.
    default_models = (
        OmegaConf.to_container(defaults.get("models", []) or [], resolve=False)
        if defaults
        else []
    )
    user_models = (
        OmegaConf.to_container(user_config.get("models", []) or [], resolve=False)
        if user_config
        else []
    )

    merged = OmegaConf.merge(defaults, user_config)

    # Name-based merge: user models override defaults, but default-only models are kept
    if default_models and user_models:
        user_model_names = {m.get("name") for m in user_models if isinstance(m, dict)}
        extra_defaults = [
            m
            for m in default_models
            if isinstance(m, dict) and m.get("name") not in user_model_names
        ]
        if extra_defaults:
            all_models = user_models + extra_defaults
            merged["models"] = OmegaConf.create(all_models)
            logger.info(
                f"Merged {len(extra_defaults)} default-only models into config: "
                f"{[m.get('name') for m in extra_defaults]}"
            )

    # Cache result
    _config_cache = merged

    logger.info("Configuration loaded successfully with OmegaConf")
    return merged


def reload_config() -> DictConfig:
    """Reload configuration from disk (invalidate cache)."""
    global _config_cache
    _config_cache = None
    return load_config(force_reload=True)


def get_backend_config(section: Optional[str] = None) -> DictConfig:
    """
    Get backend configuration section.

    Args:
        section: Optional subsection (e.g., 'diarization', 'cleanup')

    Returns:
        DictConfig for backend section or subsection
    """
    cfg = load_config()
    if "backend" not in cfg:
        return OmegaConf.create({})

    backend_cfg = cfg.backend
    if section:
        return backend_cfg.get(section, OmegaConf.create({}))
    return backend_cfg


def get_service_config(service_name: str) -> DictConfig:
    """
    Get service configuration section.

    Args:
        service_name: Service name (e.g., 'speaker_recognition', 'asr_services')

    Returns:
        DictConfig for service section
    """
    cfg = load_config()
    return cfg.get(service_name, OmegaConf.create({}))


def save_config_section(section_path: str, values: dict) -> bool:
    """
    Update a config section and save to config.yml.

    Also updates the in-memory config cache so changes take effect immediately,
    even when CONFIG_FILE points to a different file (e.g., in test environments).

    Args:
        section_path: Dot-separated path (e.g., 'backend.diarization')
        values: Dict with new values

    Returns:
        True if saved successfully
    """
    try:
        config_path = get_config_dir() / "config.yml"

        # Load existing config
        existing_config = {}
        if config_path.exists():
            existing_config = OmegaConf.load(config_path)

        # Update section using dot notation
        OmegaConf.update(existing_config, section_path, values, merge=True)

        # Save back to file
        OmegaConf.save(existing_config, config_path)

        # Reload config from the primary config file (CONFIG_FILE env var)
        merged = reload_config()

        # Also apply the values to the in-memory cache directly.
        # This is needed when CONFIG_FILE points to a different file than config.yml
        # (e.g., test configs), so the saved values still take effect at runtime.
        OmegaConf.update(merged, section_path, values, merge=True)

        logger.info(f"Saved config section '{section_path}' to {config_path}")
        return True

    except Exception as e:
        logger.error(f"Error saving config section '{section_path}': {e}")
        return False
