"""Plugin service for accessing the global plugin router.

This module provides singleton access to the plugin router, allowing
worker jobs to trigger plugins without accessing FastAPI app state directly.
"""

import importlib
import inspect
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml

from advanced_omi_backend.config_loader import get_plugins_yml_path
from advanced_omi_backend.plugins import BasePlugin, PluginRouter
from advanced_omi_backend.plugins.services import PluginServices

logger = logging.getLogger(__name__)

# Global plugin router instance
_plugin_router: Optional[PluginRouter] = None


def _get_plugins_dir() -> Path:
    """Get external plugins directory.

    Priority: PLUGINS_DIR env var > Docker path > local dev path.
    """
    env_dir = os.getenv("PLUGINS_DIR")
    if env_dir:
        return Path(env_dir)
    docker_path = Path("/app/plugins")
    if docker_path.is_dir():
        return docker_path
    # Local dev: plugin_service.py is at <repo>/backends/advanced/src/advanced_omi_backend/services/
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "plugins"


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
            if ":-" in var_expr:
                var_name, default = var_expr.split(":-", 1)
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

        return re.sub(r"\$\{([^}]+)\}", replacer, value)

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
        plugins_dir = _get_plugins_dir()
        plugin_config_path = plugins_dir / plugin_id / "config.yml"

        if plugin_config_path.exists():
            logger.debug(f"Loading plugin config from: {plugin_config_path}")
            with open(plugin_config_path, "r") as f:
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
    config["enabled"] = orchestration_config.get("enabled", False)
    config["events"] = orchestration_config.get("events", [])
    config["condition"] = orchestration_config.get("condition", {"type": "always"})

    # Add plugin ID for reference
    config["plugin_id"] = plugin_id

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


def extract_env_var_name(value: str) -> Optional[str]:
    """Extract environment variable name from ${ENV_VAR} or ${ENV_VAR:-default} syntax.

    Args:
        value: String potentially containing ${ENV_VAR} reference

    Returns:
        Environment variable name if found, None otherwise

    Examples:
        >>> extract_env_var_name('${SMTP_HOST}')
        'SMTP_HOST'
        >>> extract_env_var_name('${SMTP_PORT:-587}')
        'SMTP_PORT'
        >>> extract_env_var_name('plain text')
        None
    """
    if not isinstance(value, str):
        return None

    match = re.search(r"\$\{([^}:]+)", value)
    if match:
        return match.group(1).strip()
    return None


def infer_field_type(key: str, value: Any) -> Dict[str, Any]:
    """Infer field schema from config key and value.

    Args:
        key: Configuration field key (e.g., 'smtp_password')
        value: Configuration field value

    Returns:
        Field schema dictionary with type, label, default, etc.

    Examples:
        >>> infer_field_type('smtp_password', '${SMTP_PASSWORD}')
        {'type': 'password', 'label': 'SMTP Password', 'secret': True, 'env_var': 'SMTP_PASSWORD', 'required': True}

        >>> infer_field_type('max_sentences', 3)
        {'type': 'number', 'label': 'Max Sentences', 'default': 3}
    """
    # Generate human-readable label from key
    label = key.replace("_", " ").title()

    # Check for environment variable reference
    if isinstance(value, str) and "${" in value:
        env_var = extract_env_var_name(value)
        if not env_var:
            return {"type": "string", "label": label, "default": value}

        # Determine if this is a secret based on env var name
        secret_keywords = ["PASSWORD", "TOKEN", "KEY", "SECRET", "APIKEY", "API_KEY"]
        is_secret = any(keyword in env_var.upper() for keyword in secret_keywords)

        # Extract default value if present (${VAR:-default})
        default_value = None
        if ":-" in value:
            default_match = re.search(r":-([^}]+)", value)
            if default_match:
                default_value = default_match.group(1).strip()
                # Try to parse boolean/number defaults
                if default_value.lower() in ("true", "false"):
                    default_value = default_value.lower() == "true"
                elif default_value.isdigit():
                    default_value = int(default_value)

        schema = {
            "type": "password" if is_secret else "string",
            "label": label,
            "secret": is_secret,
            "env_var": env_var,
            "required": is_secret,  # Secrets are required
        }

        if default_value is not None:
            schema["default"] = default_value
            schema["required"] = False

        return schema

    # Boolean values
    elif isinstance(value, bool):
        return {"type": "boolean", "label": label, "default": value}

    # Numeric values
    elif isinstance(value, int):
        return {"type": "number", "label": label, "default": value}

    elif isinstance(value, float):
        return {"type": "number", "label": label, "default": value, "step": 0.1}

    # List values
    elif isinstance(value, list):
        return {"type": "array", "label": label, "default": value}

    # Object/dict values
    elif isinstance(value, dict):
        return {"type": "object", "label": label, "default": value}

    # String values (fallback)
    else:
        return {
            "type": "string",
            "label": label,
            "default": str(value) if value is not None else "",
        }


def load_schema_yml(plugin_id: str) -> Optional[Dict[str, Any]]:
    """Load optional schema.yml override for a plugin.

    Args:
        plugin_id: Plugin identifier

    Returns:
        Schema dictionary if schema.yml exists, None otherwise
    """
    try:
        plugins_dir = _get_plugins_dir()
        schema_path = plugins_dir / plugin_id / "schema.yml"

        if schema_path.exists():
            logger.debug(f"Loading schema override from: {schema_path}")
            with open(schema_path, "r") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load schema.yml for plugin '{plugin_id}': {e}")

    return None


def infer_schema_from_config(plugin_id: str, config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Infer configuration schema from plugin config.yml.

    This function analyzes the config.yml file to generate a JSON schema
    for rendering forms in the frontend. It can be overridden by providing
    a schema.yml file in the plugin directory.

    Args:
        plugin_id: Plugin identifier
        config_dict: Configuration dictionary from config.yml

    Returns:
        Schema dictionary with 'settings' and 'env_vars' sections

    Example:
        >>> config = {'subject_prefix': 'Summary', 'smtp_password': '${SMTP_PASSWORD}'}
        >>> schema = infer_schema_from_config('email_summarizer', config)
        >>> schema['settings']['subject_prefix']['type']
        'string'
        >>> schema['env_vars']['SMTP_PASSWORD']['type']
        'password'
    """
    # Check for explicit schema.yml override
    explicit_schema = load_schema_yml(plugin_id)
    if explicit_schema:
        logger.info(f"Using explicit schema.yml for plugin '{plugin_id}'")
        return explicit_schema

    # Infer schema from config values
    settings_schema = {}
    env_vars_schema = {}

    for key, value in config_dict.items():
        field_schema = infer_field_type(key, value)

        # Separate env vars from regular settings
        if field_schema.get("env_var"):
            env_var_name = field_schema["env_var"]
            env_vars_schema[env_var_name] = field_schema
        else:
            settings_schema[key] = field_schema

    return {"settings": settings_schema, "env_vars": env_vars_schema}


def mask_secrets_in_config(config: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """Mask secret values in configuration for frontend display.

    Args:
        config: Configuration dictionary with actual values
        schema: Schema dictionary identifying secret fields

    Returns:
        Configuration with secrets masked as '••••••••••••'

    Example:
        >>> config = {'smtp_password': 'actual_password'}
        >>> schema = {'env_vars': {'SMTP_PASSWORD': {'secret': True}}}
        >>> masked = mask_secrets_in_config(config, schema)
        >>> masked['smtp_password']
        '••••••••••••'
    """
    masked_config = config.copy()

    # Get list of secret environment variable names
    secret_env_vars = set()
    for env_var, field_schema in schema.get("env_vars", {}).items():
        if field_schema.get("secret", False):
            secret_env_vars.add(env_var)

    # Mask values that reference secret environment variables
    for key, value in masked_config.items():
        if isinstance(value, str):
            env_var = extract_env_var_name(value)
            if env_var and env_var in secret_env_vars:
                # Check if env var is actually set
                is_set = bool(os.environ.get(env_var))
                masked_config[key] = "••••••••••••" if is_set else ""

    return masked_config


def get_plugin_metadata(
    plugin_id: str, plugin_class: Type[BasePlugin], orchestration_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Get complete metadata for a plugin including schema and current config.

    Args:
        plugin_id: Plugin identifier
        plugin_class: Plugin class type
        orchestration_config: Orchestration config from plugins.yml

    Returns:
        Complete plugin metadata for frontend
    """
    # Load plugin config.yml
    try:
        plugins_dir = _get_plugins_dir()
        plugin_config_path = plugins_dir / plugin_id / "config.yml"

        config_dict = {}
        if plugin_config_path.exists():
            with open(plugin_config_path, "r") as f:
                config_dict = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config for plugin '{plugin_id}': {e}")
        config_dict = {}

    # Infer schema
    config_schema = infer_schema_from_config(plugin_id, config_dict)

    # Get plugin metadata from class
    plugin_name = getattr(plugin_class, "name", plugin_id.replace("_", " ").title())
    plugin_description = getattr(plugin_class, "description", "")
    supports_testing = hasattr(plugin_class, "test_connection")

    # Mask secrets in current config
    current_config = load_plugin_config(plugin_id, orchestration_config)
    masked_config = mask_secrets_in_config(current_config, config_schema)

    # Mark which env vars are set
    for env_var_name, env_var_schema in config_schema.get("env_vars", {}).items():
        env_var_schema["is_set"] = bool(os.environ.get(env_var_name))
        if env_var_schema.get("secret") and env_var_schema["is_set"]:
            env_var_schema["value"] = "••••••••••••"
        else:
            env_var_schema["value"] = os.environ.get(env_var_name, "")

    return {
        "plugin_id": plugin_id,
        "name": plugin_name,
        "description": plugin_description,
        "enabled": orchestration_config.get("enabled", False),
        "status": "active" if orchestration_config.get("enabled", False) else "disabled",
        "supports_testing": supports_testing,
        "config_schema": config_schema,
        "current_config": masked_config,
        "orchestration": {
            "enabled": orchestration_config.get("enabled", False),
            "events": orchestration_config.get("events", []),
            "condition": orchestration_config.get("condition", {"type": "always"}),
        },
    }


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

    plugins_dir = _get_plugins_dir()
    if not plugins_dir.is_dir():
        logger.warning(f"Plugins directory not found: {plugins_dir}")
        return discovered_plugins

    # Add plugins dir to sys.path so plugin packages can be imported directly
    plugins_dir_str = str(plugins_dir)
    if plugins_dir_str not in sys.path:
        sys.path.insert(0, plugins_dir_str)

    logger.info(f"Scanning for plugins in: {plugins_dir}")

    # Scan for plugin directories (skip hidden/underscore dirs)
    for item in plugins_dir.iterdir():
        if not item.is_dir() or item.name.startswith("_"):
            continue

        plugin_id = item.name
        plugin_file = item / "plugin.py"

        if not plugin_file.exists():
            logger.debug(f"Skipping '{plugin_id}': no plugin.py found")
            continue

        try:
            # Convert snake_case directory name to PascalCase class name
            # e.g., email_summarizer -> EmailSummarizerPlugin
            class_name = "".join(word.capitalize() for word in plugin_id.split("_")) + "Plugin"

            # Import the plugin package directly (it's on sys.path now)
            logger.debug(f"Attempting to import plugin: {plugin_id}")
            plugin_module = importlib.import_module(plugin_id)

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
            logger.info(f"Discovered plugin: '{plugin_id}' ({class_name})")

        except ImportError as e:
            logger.warning(f"Failed to import plugin '{plugin_id}': {e}")
        except Exception as e:
            logger.error(f"Error discovering plugin '{plugin_id}': {e}", exc_info=True)

    logger.info(f"Plugin discovery complete: {len(discovered_plugins)} plugin(s) found")
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
            with open(plugins_yml, "r") as f:
                plugins_config = yaml.safe_load(f)
                # Expand environment variables in configuration
                plugins_config = expand_env_vars(plugins_config)
                plugins_data = plugins_config.get("plugins", {})

            logger.info(
                f"🔍 Loaded plugins config with {len(plugins_data)} plugin(s): {list(plugins_data.keys())}"
            )

            # Discover all plugins via auto-discovery
            discovered_plugins = discover_plugins()

            # Initialize each plugin listed in config/plugins.yml
            for plugin_id, orchestration_config in plugins_data.items():
                logger.info(
                    f"🔍 Processing plugin '{plugin_id}', enabled={orchestration_config.get('enabled', False)}"
                )
                if not orchestration_config.get("enabled", False):
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

                    # Instantiate and register the plugin
                    plugin = plugin_class(plugin_config)

                    # Let plugin register its prompts with the prompt registry
                    try:
                        from advanced_omi_backend.prompt_registry import get_prompt_registry
                        plugin.register_prompts(get_prompt_registry())
                    except Exception as e:
                        logger.debug(f"Plugin '{plugin_id}' prompt registration skipped: {e}")

                    # Note: async initialization happens in app_factory lifespan
                    _plugin_router.register_plugin(plugin_id, plugin)
                    logger.info(f"Plugin '{plugin_id}' registered successfully")

                except Exception as e:
                    logger.error(f"Failed to register plugin '{plugin_id}': {e}", exc_info=True)

            logger.info(
                f"🎉 Plugin registration complete: {len(_plugin_router.plugins)} plugin(s) registered"
            )
        else:
            logger.info("No plugins.yml found, plugins disabled")

        # Attach PluginServices for cross-plugin and system interaction
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        services = PluginServices(router=_plugin_router, redis_url=redis_url)
        _plugin_router.set_services(services)

        return _plugin_router

    except Exception as e:
        logger.error(f"Failed to initialize plugin router: {e}", exc_info=True)
        _plugin_router = None
        return None


async def ensure_plugin_router() -> Optional[PluginRouter]:
    """Get or initialize the plugin router with all plugins initialized.

    This is the standard pattern for worker processes that need the plugin router.
    It handles the get-or-init-then-initialize sequence in one call.

    Returns:
        Initialized plugin router, or None if no plugins configured
    """
    plugin_router = get_plugin_router()
    if plugin_router:
        return plugin_router

    logger.info("Initializing plugin router in worker process...")
    plugin_router = init_plugin_router()
    if plugin_router:
        for plugin_id, plugin in plugin_router.plugins.items():
            try:
                await plugin.initialize()
                logger.info(f"Plugin '{plugin_id}' initialized")
            except Exception as e:
                logger.error(f"Failed to initialize plugin '{plugin_id}': {e}")
    return plugin_router


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
