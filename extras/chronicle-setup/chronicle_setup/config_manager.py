"""
Shared configuration manager for Chronicle.

This module provides a unified interface for reading and writing configuration
across both config.yml (source of truth) and .env (backward compatibility).

Key principles:
- config.yml is the source of truth for memory provider and model settings
- .env files are kept in sync for backward compatibility with legacy code
- All config updates should use this module to maintain consistency

Usage:
    from chronicle_setup import ConfigManager

    # Scoped to a service, so .env writes land in that service's directory
    config = ConfigManager(service_path="backend")
    provider = config.get_memory_provider()
    config.set_memory_provider("chronicle")

    # config.yml only, no service .env
    config = ConfigManager()
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import set_key as dotenv_set_key
from ruamel.yaml import YAML

from .repo import find_repo_root

logger = logging.getLogger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True


class ConfigManager:
    """Manages Chronicle configuration across config.yml and .env files."""

    def __init__(
        self, service_path: Optional[str] = None, repo_root: Optional[Path] = None
    ):
        """
        Initialize ConfigManager.

        Args:
            service_path: Path to service directory, relative to the repository root
                         (e.g. "backend"). None means config.yml only —
                         no service .env is read or written.
            repo_root: Path to repository root. If None, it is located by marker files.
        """
        if repo_root is None:
            repo_root = find_repo_root()
        self.repo_root = Path(repo_root)

        self.service_path = self.repo_root / service_path if service_path else None

        # Paths
        self.config_yml_path = self.repo_root / "config" / "config.yml"
        self.env_path = self.service_path / ".env" if self.service_path else None

        logger.debug(
            f"ConfigManager initialized: repo_root={self.repo_root}, "
            f"service_path={self.service_path}, config_yml={self.config_yml_path}"
        )

    def ensure_config_yml(self) -> None:
        """Create config.yml from template if it doesn't exist.

        Raises:
            RuntimeError: If config.yml doesn't exist and template is not found.
        """
        if self.config_yml_path.exists():
            return

        template_path = self.config_yml_path.parent / "config.yml.template"
        if not template_path.exists():
            raise RuntimeError(
                f"config.yml.template not found at {template_path}. "
                "Cannot create config.yml."
            )

        self.config_yml_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template_path, self.config_yml_path)
        logger.info(f"Created {self.config_yml_path} from template")

    def _load_config_yml(self) -> Dict[str, Any]:
        """Load config.yml file."""
        if not self.config_yml_path.exists():
            raise RuntimeError(
                f"Configuration file not found at {self.config_yml_path}. "
                "Please ensure config/config.yml exists in the repository root."
            )

        try:
            with open(self.config_yml_path, "r") as f:
                config = _yaml.load(f)
                if config is None:
                    raise RuntimeError(
                        f"Configuration file {self.config_yml_path} is empty or invalid. "
                        "Please ensure it contains valid YAML configuration."
                    )
                return config
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Failed to load configuration file {self.config_yml_path}: {e}"
            )

    def _save_config_yml(self, config: Dict[str, Any]):
        """Save config.yml file with backup."""
        try:
            # Create backup
            if self.config_yml_path.exists():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = (
                    self.config_yml_path.parent / f"config.yml.backup.{timestamp}"
                )
                shutil.copy2(self.config_yml_path, backup_path)
                logger.info(f"Backed up config.yml to {backup_path.name}")

            # Write updated config
            with open(self.config_yml_path, "w") as f:
                _yaml.dump(config, f)

            logger.info(f"Saved config.yml to {self.config_yml_path}")

        except Exception as e:
            logger.error(f"Failed to save config.yml: {e}")
            raise

    def _update_env_file(self, key: str, value: str):
        """Update a single key in .env file."""
        if self.env_path is None:
            logger.debug("No service path set, skipping .env update")
            return

        if not self.env_path.exists():
            logger.warning(f".env file not found at {self.env_path}")
            return

        try:
            # Create backup
            backup_path = f"{self.env_path}.bak"
            shutil.copy2(self.env_path, backup_path)
            logger.debug(f"Backed up .env to {backup_path}")

            # Update key using python-dotenv (handles add-or-update automatically)
            dotenv_set_key(str(self.env_path), key, value, quote_mode="never")

            # Update environment variable for current process
            os.environ[key] = value

            logger.info(f"Updated {key}={value} in .env file")

        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")
            raise

    def get_enabled_services(self) -> Dict[str, bool]:
        """Return the enabled-services map from config.yml (``services:`` section).

        This is the single source of truth for which services the lifecycle
        (services.py ``--all``) starts/stops — independent of whether a service's
        ``.env`` happens to exist.
        """
        config = self._load_config_yml()
        return dict(config.get("services", {}) or {})

    def set_enabled_services(self, services: Dict[str, bool]) -> None:
        """Write the enabled-services map to config.yml (``services:`` section).

        Args:
            services: Mapping of lifecycle service name → enabled bool. Replaces the
                whole ``services:`` section so it always reflects the latest wizard run.
        """
        config = self._load_config_yml()
        config["services"] = {name: bool(on) for name, on in services.items()}
        self._save_config_yml(config)

    def get_memory_provider(self) -> str:
        """
        Get current memory provider from config.yml.

        Returns:
            Memory provider name (chronicle)
        """
        config = self._load_config_yml()
        provider = config.get("memory", {}).get("provider", "chronicle").lower()

        return provider

    def set_memory_provider(self, provider: str) -> Dict[str, Any]:
        """
        Set memory provider in both config.yml and .env.

        This updates:
        1. config.yml: memory.provider field (source of truth)
        2. .env: MEMORY_PROVIDER variable (backward compatibility, if service_path set)

        Args:
            provider: Memory provider name (chronicle)

        Returns:
            Dict with status and details of the update

        Raises:
            ValueError: If provider is invalid
        """
        # Validate provider. Chronicle (agentic vault) is currently the only provider.
        provider = provider.lower().strip()
        valid_providers = ["chronicle"]

        if provider not in valid_providers:
            raise ValueError(
                f"Invalid provider '{provider}'. "
                f"Valid providers: {', '.join(valid_providers)}"
            )

        # Update config.yml
        config = self._load_config_yml()

        if "memory" not in config:
            config["memory"] = {}

        config["memory"]["provider"] = provider
        self._save_config_yml(config)

        # Update .env for backward compatibility (if we have a service path)
        if self.env_path and self.env_path.exists():
            self._update_env_file("MEMORY_PROVIDER", provider)

        return {
            "message": (
                f"Memory provider updated to '{provider}' in config.yml"
                f"{' and .env' if self.env_path else ''}. "
                "Please restart services for changes to take effect."
            ),
            "provider": provider,
            "config_yml_path": str(self.config_yml_path),
            "env_path": str(self.env_path) if self.env_path else None,
            "requires_restart": True,
            "status": "success",
        }

    def get_memory_config(self) -> Dict[str, Any]:
        """
        Get complete memory configuration from config.yml.

        Returns:
            Full memory configuration dict
        """
        config = self._load_config_yml()
        return config.get("memory", {})

    def update_memory_config(self, updates: Dict[str, Any]):
        """
        Update memory configuration in config.yml.

        Args:
            updates: Dict of updates to merge into memory config (deep merge)
        """
        config = self._load_config_yml()

        if "memory" not in config:
            config["memory"] = {}

        # Deep merge updates recursively
        self._deep_merge(config["memory"], updates)

        self._save_config_yml(config)

        # If provider was updated, also update .env
        if "provider" in updates and self.env_path:
            self._update_env_file("MEMORY_PROVIDER", updates["provider"])

    def update_backend_config(self, updates: Dict[str, Any]):
        """
        Update the ``backend`` section of config.yml (deep merge).

        Used for backend-scoped settings such as ASR context
        (``backend.asr.context.<model_name>``), diarization, etc.

        Args:
            updates: Dict of updates to merge into the backend config
        """
        config = self._load_config_yml()

        if "backend" not in config:
            config["backend"] = {}

        self._deep_merge(config["backend"], updates)

        self._save_config_yml(config)

    def _deep_merge(self, base: dict, updates: dict) -> None:
        """
        Recursively merge updates into base dictionary.

        Args:
            base: Base dictionary to merge into (modified in-place)
            updates: Updates to merge
        """
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                # Recursively merge nested dictionaries
                self._deep_merge(base[key], value)
            else:
                # Direct assignment for non-dict values
                base[key] = value

    def get_config_defaults(self) -> Dict[str, Any]:
        """
        Get defaults configuration from config.yml.

        Returns:
            Defaults configuration dict (llm, embedding, stt, tts, vector_store)
        """
        config = self._load_config_yml()
        return config.get("defaults", {})

    def update_config_defaults(self, updates: Dict[str, str]):
        """
        Update defaults configuration in config.yml.

        Args:
            updates: Dict of updates to merge into defaults config
                    (e.g., {"llm": "openai-llm", "embedding": "openai-embed"})
        """
        config = self._load_config_yml()

        if "defaults" not in config:
            config["defaults"] = {}

        # Update defaults
        config["defaults"].update(updates)

        self._save_config_yml(config)

    def add_or_update_model(self, model_def: Dict[str, Any]):
        """
        Add or update a model in the models list by name.

        Args:
            model_def: Model definition dict with at least a 'name' key.
        """
        config = self._load_config_yml()
        if "models" not in config:
            config["models"] = []
        # Update existing or append
        for i, m in enumerate(config["models"]):
            if m.get("name") == model_def["name"]:
                config["models"][i] = model_def
                break
        else:
            config["models"].append(model_def)
        self._save_config_yml(config)

    def _load_defaults_yml(self) -> Dict[str, Any]:
        """Load the shipped defaults.yml (read-only model/template definitions)."""
        defaults_path = self.config_yml_path.parent / "defaults.yml"
        if not defaults_path.exists():
            logger.warning(f"defaults.yml not found at {defaults_path}")
            return {}
        with open(defaults_path, "r") as f:
            return _yaml.load(f) or {}

    def sync_models_from_defaults(self, names: List[str]) -> List[str]:
        """Overwrite the named model entries in config.yml with their canonical
        definitions from defaults.yml.

        config.yml model entries override defaults.yml *by name* (see the backend
        config_loader merge), so a stale full copy of e.g. ``llamacpp-llm`` in
        config.yml silently shadows the templated default — pinning a hardcoded
        ``model_url`` that ignores ``LLM_BASE_URL`` and dropping the discovery
        keys. Re-syncing restores the env-var reference (and discovery_* keys) so
        the wizard's endpoint choice actually takes effect.

        Returns the list of model names that were synced.
        """
        defaults = self._load_defaults_yml()
        default_models = {
            m.get("name"): m
            for m in (defaults.get("models", []) or [])
            if isinstance(m, dict) and m.get("name")
        }
        synced: List[str] = []
        for name in names:
            model_def = default_models.get(name)
            if model_def is None:
                logger.warning(f"Model '{name}' not found in defaults.yml; cannot sync")
                continue
            self.add_or_update_model(model_def)
            synced.append(name)
        return synced

    def get_full_config(self) -> Dict[str, Any]:
        """
        Get complete config.yml as dictionary.

        Returns:
            Full configuration dict
        """
        return self._load_config_yml()

    def save_full_config(self, config: Dict[str, Any]):
        """
        Save complete config.yml from dictionary.

        Args:
            config: Full configuration dict to save
        """
        self._save_config_yml(config)


# Global singleton instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager(service_path: Optional[str] = None) -> ConfigManager:
    """
    Get global ConfigManager singleton instance.

    Args:
        service_path: Optional service path for .env updates.
                     If None, uses cached instance or creates new one.

    Returns:
        ConfigManager instance
    """
    global _config_manager

    if _config_manager is None or service_path is not None:
        _config_manager = ConfigManager(service_path=service_path)

    return _config_manager
