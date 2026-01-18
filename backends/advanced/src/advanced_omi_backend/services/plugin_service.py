"""Plugin service for accessing the global plugin router.

This module provides singleton access to the plugin router, allowing
worker jobs to trigger plugins without accessing FastAPI app state directly.
"""

import importlib
import inspect
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Type

import yaml

from advanced_omi_backend.config_loader import get_plugins_yml_path
from advanced_omi_backend.plugins import BasePlugin, PluginRouter

logger = logging.getLogger(__name__)

# Global plugin router instance
_plugin_router: Optional[PluginRouter] = None


def expand_env_vars(value: Any) -> Any:
    """
    Recursively expand environment variables in configuration values.

    Supports ${ENV_VAR} syntax. If the environment variable is not set,
    the original placeholder is kept.

    Args:
        value: Configuration value (can be str, dict, list, or other)

    Returns:
        Value with environment variables expanded

    Examples:
        >>> os.environ['MY_TOKEN'] = 'secret123'
        >>> expand_env_vars('token: ${MY_TOKEN}')
        'token: secret123'
        >>> expand_env_vars({'token': '${MY_TOKEN}'})
        {'token': 'secret123'}
    """
    if isinstance(value, str):
        # Pattern: ${ENV_VAR} or ${ENV_VAR:-default}
        def replacer(match):
            var_expr = match.group(1)
            # Support default values: ${VAR:-default}
            if ':-' in var_expr:
                var_name, default = var_expr.split(':-', 1)
                return os.environ.get(var_name.strip(), default.strip())
            else:
                var_name = var_expr.strip()
                env_value = os.environ.get(var_name)
                if env_value is None:
                    logger.warning(
                        f"Environment variable '{var_name}' not found, "
                        f"keeping placeholder: ${{{var_name}}}"
                    )
                    return match.group(0)  # Keep original placeholder
                return env_value

        return re.sub(r'\$\{([^}]+)\}', replacer, value)

    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]

    else:
        return value


def load_plugin_config(plugin_id: str, orchestration_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Load complete plugin configuration from multiple sources.

    Configuration is loaded and merged in this order:
    1. Plugin-specific config.yml (non-secret settings)
    2. Expand environment variables from .env (secrets)
    3. Merge orchestration settings from config/plugins.yml (enabled, events, condition)

    Args:
        plugin_id: Plugin identifier (e.g., 'email_summarizer')
        orchestration_config: Orchestration settings from config/plugins.yml

    Returns:
        Complete merged plugin configuration

    Example:
        >>> load_plugin_config('email_summarizer', {'enabled': True, 'events': [...]})
        {
            'enabled': True,
            'events': ['conversation.complete'],
            'condition': {'type': 'always'},
            'subject_prefix': 'Conversation Summary',
            'smtp_host': 'smtp.gmail.com',  # Expanded from ${SMTP_HOST}
            ...
        }
    """
    config = {}

    # 1. Load plugin-specific config.yml if it exists
    try:
        import advanced_omi_backend.plugins
        plugins_dir = Path(advanced_omi_backend.plugins.__file__).parent
        plugin_config_path = plugins_dir / plugin_id / "config.yml"

        if plugin_config_path.exists():
            logger.debug(f"Loading plugin config from: {plugin_config_path}")
            with open(plugin_config_path, 'r') as f:
                plugin_config = yaml.safe_load(f) or {}
                config.update(plugin_config)
                logger.debug(f"Loaded {len(plugin_config)} config keys for '{plugin_id}'")
        else:
            logger.debug(f"No config.yml found for plugin '{plugin_id}' at {plugin_config_path}")

    except Exception as e:
        logger.warning(f"Failed to load config.yml for plugin '{plugin_id}': {e}")

    # 2. Expand environment variables (reads from .env)
    config = expand_env_vars(config)

    # 3. Merge orchestration settings from config/plugins.yml
    config['enabled'] = orchestration_config.get('enabled', False)
    config['events'] = orchestration_config.get('events', [])
    config['condition'] = orchestration_config.get('condition', {'type': 'always'})

    # Add plugin ID for reference
    config['plugin_id'] = plugin_id

    logger.debug(
        f"Plugin '{plugin_id}' config merged: enabled={config['enabled']}, "
        f"events={config['events']}, keys={list(config.keys())}"
    )

    return config


def get_plugin_router() -> Optional[PluginRouter]:
    """Get the global plugin router instance.

    Returns:
        Plugin router instance if initialized, None otherwise
    """
    global _plugin_router
    return _plugin_router


def set_plugin_router(router: PluginRouter) -> None:
    """Set the global plugin router instance.

    This should be called during app initialization in app_factory.py.

    Args:
        router: Initialized plugin router instance
    """
    global _plugin_router
    _plugin_router = router
    logger.info("Plugin router registered with plugin service")


def discover_plugins() -> Dict[str, Type[BasePlugin]]:
    """
    Discover plugins in the plugins directory.

    Scans the plugins directory for subdirectories containing plugin.py files.
    Each plugin must:
    1. Have a plugin.py file with a class inheriting from BasePlugin
    2. Export the plugin class in __init__.py
    3. Plugin class name should match directory name in PascalCase

    Returns:
        Dictionary mapping plugin_id (directory name) to plugin class

    Example:
        plugins/
        ├── email_summarizer/
        │   ├── __init__.py  (exports EmailSummarizerPlugin)
        │   └── plugin.py    (defines EmailSummarizerPlugin)

        Returns: {'email_summarizer': EmailSummarizerPlugin}
    """
    discovered_plugins = {}

    # Get the plugins directory path
    try:
        import advanced_omi_backend.plugins
        plugins_dir = Path(advanced_omi_backend.plugins.__file__).parent
    except Exception as e:
        logger.error(f"Failed to locate plugins directory: {e}")
        return discovered_plugins

    logger.info(f"🔍 Scanning for plugins in: {plugins_dir}")

    # Skip these known system directories/files
    skip_items = {'__pycache__', '__init__.py', 'base.py', 'router.py'}

    # Scan for plugin directories
    for item in plugins_dir.iterdir():
        if not item.is_dir() or item.name in skip_items:
            continue

        plugin_id = item.name
        plugin_file = item / 'plugin.py'

        if not plugin_file.exists():
            logger.debug(f"Skipping '{plugin_id}': no plugin.py found")
            continue

        try:
            # Convert snake_case directory name to PascalCase class name
            # e.g., email_summarizer -> EmailSummarizerPlugin
            class_name = ''.join(word.capitalize() for word in plugin_id.split('_')) + 'Plugin'

            # Import the plugin module
            module_path = f'advanced_omi_backend.plugins.{plugin_id}'
            logger.debug(f"Attempting to import plugin from: {module_path}")

            # Import the plugin package (which should export the class in __init__.py)
            plugin_module = importlib.import_module(module_path)

            # Try to get the plugin class
            if not hasattr(plugin_module, class_name):
                logger.warning(
                    f"Plugin '{plugin_id}' does not export '{class_name}' in __init__.py. "
                    f"Make sure the class is exported: from .plugin import {class_name}"
                )
                continue

            plugin_class = getattr(plugin_module, class_name)

            # Validate it's a class and inherits from BasePlugin
            if not inspect.isclass(plugin_class):
                logger.warning(f"'{class_name}' in '{plugin_id}' is not a class")
                continue

            if not issubclass(plugin_class, BasePlugin):
                logger.warning(
                    f"Plugin class '{class_name}' in '{plugin_id}' does not inherit from BasePlugin"
                )
                continue

            # Successfully discovered plugin
            discovered_plugins[plugin_id] = plugin_class
            logger.info(f"✅ Discovered plugin: '{plugin_id}' ({class_name})")

        except ImportError as e:
            logger.warning(f"Failed to import plugin '{plugin_id}': {e}")
        except Exception as e:
            logger.error(f"Error discovering plugin '{plugin_id}': {e}", exc_info=True)

    logger.info(f"🎉 Plugin discovery complete: {len(discovered_plugins)} plugin(s) found")
    return discovered_plugins


def init_plugin_router() -> Optional[PluginRouter]:
    """Initialize the plugin router from configuration.

    This is called during app startup to create and register the plugin router.

    Returns:
        Initialized plugin router, or None if no plugins configured
    """
    global _plugin_router

    if _plugin_router is not None:
        logger.warning("Plugin router already initialized")
        return _plugin_router

    try:
        _plugin_router = PluginRouter()

        # Load plugin configuration
        plugins_yml = get_plugins_yml_path()
        logger.info(f"🔍 Looking for plugins config at: {plugins_yml}")
        logger.info(f"🔍 File exists: {plugins_yml.exists()}")

        if plugins_yml.exists():
            with open(plugins_yml, 'r') as f:
                plugins_config = yaml.safe_load(f)
                # Expand environment variables in configuration
                plugins_config = expand_env_vars(plugins_config)
                plugins_data = plugins_config.get('plugins', {})

            logger.info(f"🔍 Loaded plugins config with {len(plugins_data)} plugin(s): {list(plugins_data.keys())}")

            # Discover all plugins via auto-discovery
            discovered_plugins = discover_plugins()

            # Core plugin names (for informational logging only)
            CORE_PLUGIN_NAMES = {'homeassistant', 'test_event'}

            # Initialize each plugin listed in config/plugins.yml
            for plugin_id, orchestration_config in plugins_data.items():
                logger.info(f"🔍 Processing plugin '{plugin_id}', enabled={orchestration_config.get('enabled', False)}")
                if not orchestration_config.get('enabled', False):
                    continue

                try:
                    # Check if plugin was discovered
                    if plugin_id not in discovered_plugins:
                        logger.warning(
                            f"Plugin '{plugin_id}' not found. "
                            f"Make sure the plugin directory exists in plugins/ with proper structure."
                        )
                        continue

                    # Load complete plugin configuration (merges plugin config.yml + .env + orchestration)
                    plugin_config = load_plugin_config(plugin_id, orchestration_config)

                    # Get plugin class from discovered plugins
                    plugin_class = discovered_plugins[plugin_id]
                    plugin_type = "core" if plugin_id in CORE_PLUGIN_NAMES else "community"

                    # Instantiate and register the plugin
                    plugin = plugin_class(plugin_config)
                    # Note: async initialization happens in app_factory lifespan
                    _plugin_router.register_plugin(plugin_id, plugin)
                    logger.info(f"✅ Plugin '{plugin_id}' registered successfully ({plugin_type})")

                except Exception as e:
                    logger.error(f"Failed to register plugin '{plugin_id}': {e}", exc_info=True)

            logger.info(f"🎉 Plugin registration complete: {len(_plugin_router.plugins)} plugin(s) registered")
        else:
            logger.info("No plugins.yml found, plugins disabled")

        return _plugin_router

    except Exception as e:
        logger.error(f"Failed to initialize plugin router: {e}", exc_info=True)
        _plugin_router = None
        return None


async def cleanup_plugin_router() -> None:
    """Clean up the plugin router and all registered plugins."""
    global _plugin_router

    if _plugin_router:
        try:
            await _plugin_router.cleanup_all()
            logger.info("Plugin router cleanup complete")
        except Exception as e:
            logger.error(f"Error during plugin router cleanup: {e}")
        finally:
            _plugin_router = None
