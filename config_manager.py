"""
Shared configuration manager for Chronicle.

This module provides a unified interface for reading and writing configuration
across both config.yml (source of truth) and .env (backward compatibility).

Key principles:
- config.yml is the source of truth for memory provider and model settings
- .env files are kept in sync for backward compatibility with legacy code
- All config updates should use this module to maintain consistency

Usage:
    # From any service in the project
    from config_manager import ConfigManager

    # For backend service
    config = ConfigManager(service_path="backends/advanced")
    provider = config.get_memory_provider()
    config.set_memory_provider("openmemory_mcp")

    # Auto-detects paths from cwd
    config = ConfigManager()
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages Chronicle configuration across config.yml and .env files."""

    def __init__(self, service_path: Optional[str] = None, repo_root: Optional[Path] = None):
        """
        Initialize ConfigManager.

        Args:
            service_path: Path to service directory (e.g., "backends/advanced", "extras/speaker-recognition").
                         If None, auto-detects from current working directory.
            repo_root: Path to repository root. If None, auto-detects by finding config.yml.
        """
        # Find repo root
        if repo_root is None:
            repo_root = self._find_repo_root()
        self.repo_root = Path(repo_root)

        # Find service directory
        if service_path is None:
            service_path = self._detect_service_path()
        self.service_path = self.repo_root / service_path if service_path else None

        # Paths
        self.config_yml_path = self.repo_root / "config" / "config.yml"
        self.env_path = self.service_path / ".env" if self.service_path else None

        logger.debug(f"ConfigManager initialized: repo_root={self.repo_root}, "
                    f"service_path={self.service_path}, config_yml={self.config_yml_path}")

    def _find_repo_root(self) -> Path:
        """Find repository root using __file__ location (config_manager.py is always at repo root)."""
        return Path(__file__).parent

    def _detect_service_path(self) -> Optional[str]:
        """Auto-detect service path from current working directory."""
        cwd = Path.cwd()

        # Check if we're in a known service directory
        known_services = [
            "backends/advanced",
            "extras/speaker-recognition",
            "extras/openmemory-mcp",
            "extras/asr-services",
        ]

        for service in known_services:
            service_full_path = self.repo_root / service
            if cwd == service_full_path or str(cwd).startswith(str(service_full_path)):
                return service

        logger.debug("Could not auto-detect service path from cwd")
        return None

    def _load_config_yml(self) -> Dict[str, Any]:
        """Load config.yml file."""
        if not self.config_yml_path.exists():
            raise RuntimeError(
                f"Configuration file not found at {self.config_yml_path}. "
                "Please ensure config/config.yml exists in the repository root."
            )

        try:
            with open(self.config_yml_path, 'r') as f:
                config = yaml.safe_load(f)
                if config is None:
                    raise RuntimeError(
                        f"Configuration file {self.config_yml_path} is empty or invalid. "
                        "Please ensure it contains valid YAML configuration."
                    )
                return config
        except yaml.YAMLError as e:
            raise RuntimeError(
                f"Invalid YAML in configuration file {self.config_yml_path}: {e}"
            )
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
                backup_path = self.config_yml_path.parent / f"config.yml.backup.{timestamp}"
                shutil.copy2(self.config_yml_path, backup_path)
                logger.info(f"Backed up config.yml to {backup_path.name}")

            # Write updated config
            with open(self.config_yml_path, 'w') as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

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
            # Read current .env
            with open(self.env_path, 'r') as f:
                lines = f.readlines()

            # Update or add line
            key_found = False
            updated_lines = []

            for line in lines:
                if line.strip().startswith(f"{key}="):
                    updated_lines.append(f"{key}={value}\n")
                    key_found = True
                else:
                    updated_lines.append(line)

            # If key wasn't found, add it
            if not key_found:
                updated_lines.append(f"\n# Auto-updated by ConfigManager\n{key}={value}\n")

            # Create backup
            backup_path = f"{self.env_path}.bak"
            shutil.copy2(self.env_path, backup_path)
            logger.debug(f"Backed up .env to {backup_path}")

            # Write updated file
            with open(self.env_path, 'w') as f:
                f.writelines(updated_lines)

            # Update environment variable for current process
            os.environ[key] = value

            logger.info(f"Updated {key}={value} in .env file")

        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")
            raise

    def get_memory_provider(self) -> str:
        """
        Get current memory provider from config.yml.

        Returns:
            Memory provider name (chronicle, openmemory_mcp, or mycelia)
        """
        config = self._load_config_yml()
        provider = config.get("memory", {}).get("provider", "chronicle").lower()

        # Map legacy names
        if provider in ("friend-lite", "friend_lite"):
            provider = "chronicle"

        return provider

    def set_memory_provider(self, provider: str) -> Dict[str, Any]:
        """
        Set memory provider in both config.yml and .env.

        This updates:
        1. config.yml: memory.provider field (source of truth)
        2. .env: MEMORY_PROVIDER variable (backward compatibility, if service_path set)

        Args:
            provider: Memory provider name (chronicle, openmemory_mcp, or mycelia)

        Returns:
            Dict with status and details of the update

        Raises:
            ValueError: If provider is invalid
        """
        # Validate provider
        provider = provider.lower().strip()
        valid_providers = ["chronicle", "openmemory_mcp", "mycelia"]

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
            "status": "success"
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
