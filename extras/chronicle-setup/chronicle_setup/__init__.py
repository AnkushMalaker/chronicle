"""Shared helpers for Chronicle's setup path.

``wizard.py`` and every service ``init.py`` need the same handful of things:
read what is already in a ``.env``, prompt for a value without making the user
retype secrets, probe the host for Tailscale/CUDA, and mint a long-lived API key.

This is a real package so those scripts can simply import it. It used to be a
single ``setup_utils.py`` at the repository root that each script reached by
appending the repo root to ``sys.path`` — which only worked when the script was
launched from its own directory.

Import from the top level; the submodule split is an implementation detail::

    from chronicle_setup import read_env_value, prompt_with_existing_masked
"""

from .checks import (
    FAIL,
    NOT_APPLICABLE,
    OK,
    WARN,
    CheckContext,
    CheckResult,
    run_all_checks,
    worst_status,
)
from .chronicle_api import mint_chronicle_api_key
from .compose import update_caddy_socket_override
from .config_manager import ConfigManager
from .env import is_placeholder, mask_value, read_env_value, resolve_ingest_config
from .prompts import (
    prompt_api_key,
    prompt_password,
    prompt_token,
    prompt_value,
    prompt_with_existing_masked,
)
from .repo import find_repo_root, looks_like_repo_root
from .system import (
    cert_needs_renewal,
    decide_cert_mode,
    detect_cuda_version,
    detect_lan_ip,
    detect_tailscale_info,
    enable_tailscaled_at_boot,
    ensure_tailscale_cert,
    generate_tailscale_certs,
    list_tailnet_peers,
    tailscale_socket_path,
    tailscaled_enabled_at_boot,
)

__all__ = [
    "CheckContext",
    "CheckResult",
    "ConfigManager",
    "FAIL",
    "NOT_APPLICABLE",
    "OK",
    "WARN",
    "cert_needs_renewal",
    "decide_cert_mode",
    "detect_cuda_version",
    "detect_lan_ip",
    "detect_tailscale_info",
    "enable_tailscaled_at_boot",
    "ensure_tailscale_cert",
    "find_repo_root",
    "generate_tailscale_certs",
    "list_tailnet_peers",
    "is_placeholder",
    "looks_like_repo_root",
    "mask_value",
    "mint_chronicle_api_key",
    "prompt_api_key",
    "prompt_password",
    "prompt_token",
    "prompt_value",
    "prompt_with_existing_masked",
    "read_env_value",
    "resolve_ingest_config",
    "run_all_checks",
    "tailscale_socket_path",
    "tailscaled_enabled_at_boot",
    "update_caddy_socket_override",
    "worst_status",
]
