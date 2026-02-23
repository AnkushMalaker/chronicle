import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import yaml
from omegaconf import OmegaConf

# Add repo root to path to import config_manager
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config_manager import ConfigManager


class ConfigTestHelper:
    """Helper library for testing configuration logic."""

    def _to_dict(self, obj: Any) -> Any:
        """Recursively converts Robot Framework DotDict to standard dict."""
        if isinstance(obj, dict):
            return {k: self._to_dict(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._to_dict(v) for v in obj]
        return obj

    def resolve_omega_config(
        self, config_template: Dict[str, Any], env_vars: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Resolves an OmegaConf configuration template with provided environment variables.
        """
        config_template = self._to_dict(config_template)
        # We need to ensure values are strings for os.environ
        str_env_vars = {k: str(v) for k, v in env_vars.items()}

        with patch.dict(os.environ, str_env_vars):
            conf = OmegaConf.create(config_template)
            resolved = OmegaConf.to_container(conf, resolve=True)
            return resolved

    def check_url_parsing(self, url: str) -> Dict[str, Any]:
        """
        Parses a URL and returns its components to verify correct parsing.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return {"scheme": parsed.scheme, "netloc": parsed.netloc, "path": parsed.path}

    def create_temp_config_structure(
        self, base_path: str, content: Dict[str, Any]
    ) -> str:
        """
        Creates the config folder structure and config.yml within the given base path.
        """
        content = self._to_dict(content)
        path = Path(base_path) / "config"
        path.mkdir(parents=True, exist_ok=True)
        config_file = path / "config.yml"
        with open(config_file, "w") as f:
            yaml.dump(content, f, default_flow_style=False, sort_keys=False)
        return str(base_path)

    def get_config_manager_instance(self, repo_root: str) -> ConfigManager:
        """Returns a ConfigManager instance configured with the given repo_root."""
        return ConfigManager(service_path=None, repo_root=Path(repo_root))

    def add_model_to_config_manager(self, cm: ConfigManager, model_def: Dict[str, Any]):
        """Wrapper for add_or_update_model that converts arguments."""
        model_def = self._to_dict(model_def)
        cm.add_or_update_model(model_def)

    def update_defaults_in_config_manager(
        self, cm: ConfigManager, updates: Dict[str, str]
    ):
        """Wrapper for update_config_defaults that converts arguments."""
        updates = self._to_dict(updates)
        cm.update_config_defaults(updates)
