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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml

from advanced_omi_backend.config_loader import get_plugins_yml_path
from advanced_omi_backend.plugins import BasePlugin, PluginRouter
from advanced_omi_backend.plugins.services import PluginServices

logger = logging.getLogger(__name__)

# Global plugin router instance
_plugin_router: Optional[PluginRouter] = None

# Redis key for signaling worker restart (consumed by orchestrator's HealthMonitor)
WORKER_RESTART_KEY = "chronicle:worker_restart_requested"


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


def load_plugin_env(plugin_id: str) -> Dict[str, str]:
    """Load per-plugin .env file from plugins/{id}/.env.

    Parses KEY=value lines, skipping comments and blank lines.
    Strips surrounding quotes from values.

    Args:
        plugin_id: Plugin identifier (directory name)

    Returns:
        Dict of env var names to values. Empty dict if file doesn't exist.
    """
    plugins_dir = _get_plugins_dir()
    env_path = plugins_dir / plugin_id / ".env"

    if not env_path.exists():
        return {}

    env_vars: Dict[str, str] = {}
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                env_vars[key] = value
    except Exception as e:
        logger.warning(f"Failed to read plugin .env for '{plugin_id}': {e}")

    return env_vars


def save_plugin_env(plugin_id: str, env_vars: Dict[str, str]) -> Path:
    """Save environment variables to plugins/{id}/.env.

    Merges new values into existing per-plugin .env file.
    Creates the file if it doesn't exist.

    Args:
        plugin_id: Plugin identifier (directory name)
        env_vars: Dict of env var names to values to write

    Returns:
        Path to the written .env file
    """
    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id
    env_path = plugin_dir / ".env"

    # Load existing values and merge
    existing = load_plugin_env(plugin_id)
    existing.update(env_vars)

    # Ensure plugin directory exists
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # Write all values
    with open(env_path, "w") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")

    logger.info(f"Saved {len(env_vars)} env var(s) to {env_path}")
    return env_path


def expand_env_vars(value: Any, extra_env: Optional[Dict[str, str]] = None) -> Any:
    """
    Recursively expand environment variables in configuration values.

    Supports ${ENV_VAR} syntax. Checks extra_env first (if provided),
    then falls back to os.environ. If neither has the variable,
    the original placeholder is kept.

    Args:
        value: Configuration value (can be str, dict, list, or other)
        extra_env: Optional dict of additional env vars to check before os.environ

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
                var_name = var_name.strip()
                if extra_env and var_name in extra_env:
                    return extra_env[var_name]
                return os.environ.get(var_name, default.strip())
            else:
                var_name = var_expr.strip()
                if extra_env and var_name in extra_env:
                    return extra_env[var_name]
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
        return {k: expand_env_vars(v, extra_env=extra_env) for k, v in value.items()}

    elif isinstance(value, list):
        return [expand_env_vars(item, extra_env=extra_env) for item in value]

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

    # 2. Expand environment variables (per-plugin .env first, then os.environ)
    plugin_env = load_plugin_env(plugin_id)
    config = expand_env_vars(config, extra_env=plugin_env)

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


def mask_secrets_in_config(
    config: Dict[str, Any],
    schema: Dict[str, Any],
    plugin_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Mask secret values in configuration for frontend display.

    Args:
        config: Configuration dictionary with actual values
        schema: Schema dictionary identifying secret fields
        plugin_env: Optional per-plugin env vars (checked before os.environ)

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
                # Check if env var is set in per-plugin .env or os.environ
                is_set = bool(
                    (plugin_env and plugin_env.get(env_var))
                    or os.environ.get(env_var)
                )
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

    # Load per-plugin env vars
    plugin_env = load_plugin_env(plugin_id)

    # Mask secrets in current config
    current_config = load_plugin_config(plugin_id, orchestration_config)
    masked_config = mask_secrets_in_config(current_config, config_schema, plugin_env=plugin_env)

    # Mark which env vars are set (check per-plugin .env first, then os.environ)
    for env_var_name, env_var_schema in config_schema.get("env_vars", {}).items():
        resolved = plugin_env.get(env_var_name) or os.environ.get(env_var_name)
        env_var_schema["is_set"] = bool(resolved)
        if env_var_schema.get("secret") and env_var_schema["is_set"]:
            env_var_schema["value"] = "••••••••••••"
        else:
            env_var_schema["value"] = resolved or ""

    # Determine runtime health status from the live router
    health_status = "unknown"
    health_error = None
    router = get_plugin_router()
    if router and plugin_id in router.plugin_health:
        h = router.plugin_health[plugin_id]
        health_status = h.status
        health_error = h.error
    elif not orchestration_config.get("enabled", False):
        health_status = "disabled"

    result = {
        "plugin_id": plugin_id,
        "name": plugin_name,
        "description": plugin_description,
        "enabled": orchestration_config.get("enabled", False),
        "status": health_status,
        "supports_testing": supports_testing,
        "config_schema": config_schema,
        "current_config": masked_config,
        "orchestration": {
            "enabled": orchestration_config.get("enabled", False),
            "events": orchestration_config.get("events", []),
            "condition": orchestration_config.get("condition", {"type": "always"}),
        },
    }
    if health_error:
        result["error"] = health_error
    return result


def discover_plugins() -> Dict[str, Type[BasePlugin]]:
    """
    Discover plugins in the plugins directory.

    Scans the plugins directory for subdirectories containing plugin.py files.
    Each plugin must:
    1. Have a plugin.py file with a class inheriting from BasePlugin
    2. Export exactly one BasePlugin subclass in __init__.py

    Discovery works by scanning module exports for BasePlugin subclasses,
    so no naming convention between directory name and class name is required.

    Returns:
        Dictionary mapping plugin_id (directory name) to plugin class

    Example:
        plugins/
        ├── homeassistant/
        │   ├── __init__.py  (exports HomeAssistantPlugin)
        │   └── plugin.py    (defines HomeAssistantPlugin)

        Returns: {'homeassistant': HomeAssistantPlugin}
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

    # Scan for plugin directories in deterministic order (skip hidden/underscore dirs)
    for item in sorted(plugins_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(("_", ".")):
            continue

        plugin_id = item.name
        plugin_file = item / "plugin.py"

        if not plugin_file.exists():
            logger.debug(f"Skipping '{plugin_id}': no plugin.py found")
            continue

        try:
            # Import the plugin package directly (it's on sys.path now)
            logger.debug(f"Attempting to import plugin: {plugin_id}")
            plugin_module = importlib.import_module(plugin_id)

            # Scan module exports for BasePlugin subclasses, deduplicate by id()
            seen_ids = set()
            plugin_classes = []
            for attr_name in dir(plugin_module):
                attr = getattr(plugin_module, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, BasePlugin)
                    and attr is not BasePlugin
                    and id(attr) not in seen_ids
                ):
                    seen_ids.add(id(attr))
                    plugin_classes.append(attr)

            if len(plugin_classes) == 0:
                logger.warning(
                    f"Plugin '{plugin_id}': no BasePlugin subclass found in __init__.py. "
                    f"Make sure to export your plugin class: from .plugin import YourPlugin"
                )
                continue

            if len(plugin_classes) > 1:
                class_names = [cls.__name__ for cls in plugin_classes]
                logger.warning(
                    f"Plugin '{plugin_id}': found multiple BasePlugin subclasses "
                    f"{class_names}, expected exactly 1. Using first: {class_names[0]}"
                )

            plugin_class = plugin_classes[0]
            discovered_plugins[plugin_id] = plugin_class
            logger.info(f"Discovered plugin: '{plugin_id}' ({plugin_class.__name__})")

        except ImportError as e:
            logger.warning(f"Failed to import plugin '{plugin_id}': {e}")
        except Exception as e:
            logger.error(f"Error discovering plugin '{plugin_id}': {e}", exc_info=True)

    logger.info(f"Plugin discovery complete: {len(discovered_plugins)} plugin(s) found")
    return discovered_plugins


def _build_plugin_router() -> Optional[PluginRouter]:
    """Build a new plugin router from configuration without touching the global.

    This is the internal builder used by both init_plugin_router() (first startup)
    and reload_plugins() (hot-reload). It never reads or writes _plugin_router.

    Returns:
        Fully-built plugin router with plugins registered (but not yet async-initialized),
        or None if construction fails
    """
    try:
        router = PluginRouter()

        # Load plugin configuration
        plugins_yml = get_plugins_yml_path()
        logger.info(f"Looking for plugins config at: {plugins_yml}")
        logger.info(f"File exists: {plugins_yml.exists()}")

        if plugins_yml.exists():
            with open(plugins_yml, "r") as f:
                plugins_config = yaml.safe_load(f)
                # Expand environment variables in configuration
                plugins_config = expand_env_vars(plugins_config)
                plugins_data = plugins_config.get("plugins", {})

            logger.info(
                f"Loaded plugins config with {len(plugins_data)} plugin(s): {list(plugins_data.keys())}"
            )

            # Discover all plugins via auto-discovery
            discovered_plugins = discover_plugins()

            # Initialize each plugin listed in config/plugins.yml
            for plugin_id, orchestration_config in plugins_data.items():
                logger.info(
                    f"Processing plugin '{plugin_id}', enabled={orchestration_config.get('enabled', False)}"
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

                    # Note: async initialization happens in app_factory lifespan or reload_plugins
                    router.register_plugin(plugin_id, plugin)
                    logger.info(f"Plugin '{plugin_id}' registered successfully")

                except Exception as e:
                    logger.error(f"Failed to register plugin '{plugin_id}': {e}", exc_info=True)

            logger.info(
                f"Plugin registration complete: {len(router.plugins)} plugin(s) registered"
            )
        else:
            logger.info("No plugins.yml found, plugins disabled")

        # Attach PluginServices for cross-plugin and system interaction
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        services = PluginServices(router=router, redis_url=redis_url)
        router.set_services(services)

        return router

    except Exception as e:
        logger.error(f"Failed to build plugin router: {e}", exc_info=True)
        return None


def init_plugin_router() -> Optional[PluginRouter]:
    """Initialize the plugin router from configuration.

    This is called during app startup to create and install the global plugin router.
    For hot-reload, use reload_plugins() instead.

    Returns:
        Initialized plugin router, or None if no plugins configured
    """
    global _plugin_router

    if _plugin_router is not None:
        logger.warning("Plugin router already initialized")
        return _plugin_router

    router = _build_plugin_router()
    if router:
        _plugin_router = router
        logger.info("Plugin router installed as global singleton")
    return _plugin_router


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
                plugin_router.mark_plugin_initialized(plugin_id)
                logger.info(f"Plugin '{plugin_id}' initialized")
            except Exception as e:
                plugin_router.mark_plugin_failed(plugin_id, str(e))
                logger.error(f"Failed to initialize plugin '{plugin_id}': {e}")
    return plugin_router


async def cleanup_plugin_router() -> None:
    """Clean up the plugin router and all registered plugins."""
    global _plugin_router

    if _plugin_router:
        try:
            if _plugin_router._services:
                await _plugin_router._services.cleanup()
            await _plugin_router.cleanup_all()
            logger.info("Plugin router cleanup complete")
        except Exception as e:
            logger.error(f"Error during plugin router cleanup: {e}")
        finally:
            _plugin_router = None


async def reload_plugins(app=None) -> Dict[str, Any]:
    """Hot-reload all plugins by building a new router and atomically swapping it in.

    The old router continues serving requests while the new one is being built.
    The global _plugin_router is only replaced once the new router is fully
    initialized, so concurrent callers of get_plugin_router() never see None.

    Steps:
    1. Purge sys.modules entries for plugin packages (so importlib re-reads from disk)
    2. Build and initialize a new router (old router still active)
    3. Atomic swap: replace global _plugin_router with the new router
    4. Clean up old plugin instances (close SMTP, HA sessions, etc.)
    5. Update app.state if app provided

    Args:
        app: Optional FastAPI app instance to update app.state.plugin_router

    Returns:
        Result dict with reload status, counts, and timing
    """
    global _plugin_router
    start = time.monotonic()

    old_router = _plugin_router
    old_count = len(old_router.plugins) if old_router else 0

    # 1. Purge sys.modules for plugin packages only (before re-importing)
    plugins_dir = _get_plugins_dir()
    purged_modules = []
    if plugins_dir.is_dir():
        plugin_names = {
            item.name
            for item in plugins_dir.iterdir()
            if item.is_dir() and not item.name.startswith(("_", "."))
        }
        for mod_name in list(sys.modules.keys()):
            top_level = mod_name.split(".")[0]
            if top_level in plugin_names:
                del sys.modules[mod_name]
                purged_modules.append(mod_name)
        if purged_modules:
            logger.info(f"Purged {len(purged_modules)} cached plugin modules")

    # 2. Build a new router (old router still serves requests during this)
    new_router = _build_plugin_router()

    # 3. Initialize each plugin on the new router
    initialized = []
    failed = []
    if new_router:
        for plugin_id, plugin in new_router.plugins.items():
            try:
                await plugin.initialize()
                new_router.mark_plugin_initialized(plugin_id)
                initialized.append(plugin_id)
            except Exception as e:
                new_router.mark_plugin_failed(plugin_id, str(e))
                failed.append({"plugin_id": plugin_id, "error": str(e)})
                logger.error(f"Failed to initialize plugin '{plugin_id}': {e}")

    # 4. Atomic swap — from this point, all callers see the new router
    _plugin_router = new_router

    # 5. Update app.state if provided
    if app and new_router:
        app.state.plugin_router = new_router

    # 6. Clean up old router *after* the swap (best-effort, never blocks the new router)
    if old_router:
        try:
            if old_router._services:
                await old_router._services.cleanup()
            await old_router.cleanup_all()
        except Exception as e:
            logger.warning(f"Error during old plugin router cleanup: {e}")

    elapsed = time.monotonic() - start
    new_count = len(new_router.plugins) if new_router else 0

    result = {
        "success": True,
        "previous_plugin_count": old_count,
        "new_plugin_count": new_count,
        "initialized": initialized,
        "failed": failed,
        "purged_modules": len(purged_modules),
        "elapsed_seconds": round(elapsed, 3),
    }
    logger.info(
        f"Plugin reload complete: {new_count} plugins loaded "
        f"({len(initialized)} initialized, {len(failed)} failed) in {elapsed:.3f}s"
    )
    return result


def signal_worker_restart() -> None:
    """Write a Redis key to signal the worker orchestrator to restart all workers.

    The orchestrator's HealthMonitor polls for this key and triggers a restart
    when found. The key is consumed (deleted) after the restart is initiated.

    Uses its own short-lived Redis connection so it works regardless of the
    plugin router's lifecycle (e.g. during or after a failed reload).
    """
    try:
        import redis

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        client = redis.from_url(redis_url, decode_responses=True)
        try:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            client.set(WORKER_RESTART_KEY, timestamp)
            logger.info(f"Worker restart signal sent via Redis key '{WORKER_RESTART_KEY}'")
        finally:
            client.close()
    except Exception as e:
        logger.error(f"Failed to send worker restart signal: {e}")
