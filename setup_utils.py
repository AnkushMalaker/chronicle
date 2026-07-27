"""
Shared utilities for Chronicle setup scripts.

Provides common functions for interactive configuration, password masking,
and environment file handling. Used by wizard.py, init.py scripts, and plugin setup.
"""

import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import get_key


def read_env_value(env_file_path: str, key: str) -> Optional[str]:
    """
    Read a value from an .env file using python-dotenv.

    Args:
        env_file_path: Path to .env file
        key: Environment variable name

    Returns:
        Value if found, None otherwise

    Example:
        >>> value = read_env_value('.env', 'SMTP_HOST')
        >>> print(value)  # 'smtp.gmail.com' or None
    """
    env_path = Path(env_file_path)
    if not env_path.exists():
        return None

    value = get_key(str(env_path), key)
    # get_key returns None if key doesn't exist or value is empty
    return value if value else None


def resolve_ingest_config(
    search_paths: list,
    host: str = "host.docker.internal",
    default_port: str = "8000",
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the cross-service System-Errors ingest URL + token for a sidecar.

    Sidecar services (ASR, speaker-recognition) push their ERROR/CRITICAL logs to the
    backend's ``POST /api/admin/system-events/ingest`` so failures surface on the admin
    "System Errors" page instead of being buried in container logs. This sources the
    backend address + auth token the same way other shared secrets are sourced: from
    the backend .env (canonical hub on a main machine) or the repo-root .env (per-node
    store), in the given order.

    The token prefers a dedicated ``SYSTEM_EVENT_INGEST_TOKEN`` and falls back to
    ``SERVICE_MANAGER_TOKEN`` (which the backend accepts as the ingest fallback).

    Returns ``(ingest_url, ingest_token)``. Both are ``None`` when no backend config is
    found locally (e.g. a remote service node with no backend .env) — the reporter is
    opt-in and stays a no-op until both are set, so callers should only write non-None
    values and leave the keys untouched otherwise.

    Args:
        search_paths: .env paths to search, in priority order.
        host: Hostname the sidecar uses to reach the backend (default reaches the
            host gateway from inside a container).
        default_port: Backend HTTP port to use when ``BACKEND_PUBLIC_PORT`` is absent.

    Example:
        >>> url, token = resolve_ingest_config(["../../backends/advanced/.env", "../../.env"])
    """
    for path in search_paths:
        token = read_env_value(path, "SYSTEM_EVENT_INGEST_TOKEN") or read_env_value(
            path, "SERVICE_MANAGER_TOKEN"
        )
        if token:
            port = read_env_value(path, "BACKEND_PUBLIC_PORT") or default_port
            url = f"http://{host}:{port}/api/admin/system-events/ingest"
            return url, token
    return None, None


def is_placeholder(value: str, *placeholder_variants: str) -> bool:
    """
    Check if a value is a placeholder.

    Normalizes both the value and placeholders (treats hyphens/underscores as equivalent).

    Args:
        value: The value to check
        placeholder_variants: One or more placeholder strings to check against

    Returns:
        True if value matches any placeholder variant

    Example:
        >>> is_placeholder('your-key-here', 'your_key_here')
        True
        >>> is_placeholder('sk-abc123', 'your_key_here')
        False
    """
    if not value:
        return True

    # Normalize by replacing hyphens with underscores
    normalized_value = value.replace("-", "_").lower()

    for placeholder in placeholder_variants:
        normalized_placeholder = placeholder.replace("-", "_").lower()
        if normalized_value == normalized_placeholder:
            return True

    return False


def mask_value(value: str, show_chars: int = 5) -> str:
    """
    Mask a sensitive value, showing only first and last few characters.

    Args:
        value: The value to mask
        show_chars: Number of characters to show at start/end (default: 5)

    Returns:
        Masked string in format: "first5***********last5". Values too short to
        safely show partial characters (e.g. human-length passwords) are fully
        masked — every caller passes secrets, so leaking a short one verbatim
        is worse than showing nothing.

    Examples:
        >>> mask_value('sk-proj-abc123def456ghi789')
        'sk-pr***************i789'
        >>> mask_value('short')
        '*****'
        >>> mask_value('smtp_password_12345')
        'smtp_***********2345'
    """
    # Strip whitespace before processing
    value_clean = value.strip() if value else value

    if not value_clean:
        return value
    if len(value_clean) <= show_chars * 2:
        return "*" * len(value_clean)

    return f"{value_clean[:show_chars]}{'*' * min(15, len(value_clean) - show_chars * 2)}{value_clean[-show_chars:]}"


def prompt_value(prompt_text: str, default: str = "") -> str:
    """
    Prompt user for a value with optional default.

    Args:
        prompt_text: The prompt to display
        default: Default value if user presses Enter

    Returns:
        User input or default value

    Example:
        >>> email = prompt_value("Admin email", "admin@example.com")
    """
    try:
        if default:
            value = input(f"{prompt_text} [{default}]: ").strip()
            return value if value else default
        else:
            return input(f"{prompt_text}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default


def prompt_password(
    prompt_text: str, min_length: int = 8, allow_generated: bool = False
) -> str:
    """
    Prompt user for a password (hidden input).

    Args:
        prompt_text: The prompt to display
        min_length: Minimum password length (default: 8)
        allow_generated: If True, generate secure password in non-interactive mode

    Returns:
        Password entered by user or generated password

    Example:
        >>> password = prompt_password("Admin password")
        >>> api_key = prompt_password("API Key", min_length=0)  # No length requirement
    """
    while True:
        try:
            password = getpass.getpass(f"{prompt_text}: ")
            if len(password) >= min_length:
                return password
            if min_length > 0:
                print(f"[WARNING] Password must be at least {min_length} characters")
        except (EOFError, KeyboardInterrupt):
            if allow_generated:
                # Non-interactive environment - generate secure password
                print("[WARNING] Non-interactive environment detected")
                password = f"generated-{secrets.token_hex(8)}"
                print(f"Generated secure password: {password}")
                return password
            else:
                # Return empty string if generation not allowed
                return ""


def prompt_with_existing_masked(
    prompt_text: str,
    existing_value: Optional[str] = None,
    placeholders: Optional[List[str]] = None,
    is_password: bool = False,
    default: str = "",
    env_file_path: Optional[str] = None,
    env_key: Optional[str] = None,
) -> str:
    """
    Prompt for a value, showing masked existing value if present.

    This is the primary function for plugins to use when prompting for secrets.
    It automatically:
    - Reads existing value from .env if env_file_path and env_key provided
    - Masks sensitive values when displaying
    - Allows user to press Enter to keep existing value
    - Falls back to default if no existing value

    Args:
        prompt_text: The prompt to display
        existing_value: Existing value (or None to auto-read from .env)
        placeholders: List of placeholder values to treat as "not set"
        is_password: Whether to use password input and masking
        default: Default value if no existing value
        env_file_path: Path to .env file (for auto-reading existing value)
        env_key: Environment variable name (for auto-reading existing value)

    Returns:
        User input, existing value, or default

    Examples:
        >>> # Basic usage with explicit existing value
        >>> api_key = prompt_with_existing_masked(
        ...     "OpenAI API Key",
        ...     existing_value="sk-abc123",
        ...     is_password=True
        ... )

        >>> # Auto-read from .env
        >>> smtp_password = prompt_with_existing_masked(
        ...     "SMTP Password",
        ...     env_file_path=".env",
        ...     env_key="SMTP_PASSWORD",
        ...     placeholders=['your-password-here'],
        ...     is_password=True
        ... )

        >>> # Plugin setup example
        >>> ha_token = prompt_with_existing_masked(
        ...     "Home Assistant Token",
        ...     env_file_path="../../.env",
        ...     env_key="HA_TOKEN",
        ...     placeholders=['your-token-here'],
        ...     is_password=True
        ... )
    """
    placeholders = placeholders or []

    # Auto-read existing value from .env if parameters provided
    if existing_value is None and env_file_path and env_key:
        existing_value = read_env_value(env_file_path, env_key)

    # Check if we have a valid existing value (not a placeholder)
    has_valid_existing = existing_value and not is_placeholder(
        existing_value, *placeholders
    )

    if has_valid_existing:
        # Show masked value with option to reuse
        if is_password:
            masked = mask_value(existing_value)
            display_prompt = (
                f"{prompt_text} ({masked}) [press Enter to reuse, or enter new]"
            )
        else:
            display_prompt = (
                f"{prompt_text} ({existing_value}) [press Enter to reuse, or enter new]"
            )

        if is_password:
            user_input = prompt_password(display_prompt, min_length=0)
        else:
            user_input = prompt_value(display_prompt, "")

        # If user pressed Enter, keep existing value
        return user_input if user_input else existing_value
    else:
        # No existing value, prompt normally
        if is_password:
            return prompt_password(prompt_text, min_length=0)
        else:
            return prompt_value(prompt_text, default)


# Convenience functions for common patterns


def prompt_api_key(
    service_name: str,
    env_file_path: str = ".env",
    env_key: Optional[str] = None,
    placeholders: Optional[List[str]] = None,
) -> str:
    """
    Convenience function for prompting API keys.

    Args:
        service_name: Human-readable service name (e.g., "OpenAI", "Deepgram")
        env_file_path: Path to .env file
        env_key: Environment variable name (defaults to {SERVICE}_API_KEY)
        placeholders: Custom placeholders (defaults to common API key placeholders)

    Returns:
        API key value

    Example:
        >>> api_key = prompt_api_key("OpenAI", env_file_path="../../.env")
    """
    env_key = env_key or f"{service_name.upper().replace(' ', '_')}_API_KEY"
    placeholders = placeholders or [
        "your-api-key-here",
        "your_api_key_here",
        f"your-{service_name.lower()}-key-here",
    ]

    return prompt_with_existing_masked(
        prompt_text=f"{service_name} API Key",
        env_file_path=env_file_path,
        env_key=env_key,
        placeholders=placeholders,
        is_password=True,
    )


def prompt_token(
    service_name: str,
    env_file_path: str = ".env",
    env_key: Optional[str] = None,
    placeholders: Optional[List[str]] = None,
) -> str:
    """
    Convenience function for prompting authentication tokens.

    Args:
        service_name: Human-readable service name (e.g., "Home Assistant", "GitHub")
        env_file_path: Path to .env file
        env_key: Environment variable name (defaults to {SERVICE}_TOKEN)
        placeholders: Custom placeholders (defaults to common token placeholders)

    Returns:
        Token value

    Example:
        >>> ha_token = prompt_token("Home Assistant", env_file_path="../../.env")
    """
    env_key = env_key or f"{service_name.upper().replace(' ', '_')}_TOKEN"
    placeholders = placeholders or [
        "your-token-here",
        "your_token_here",
        f"your-{service_name.lower()}-token-here",
    ]

    return prompt_with_existing_masked(
        prompt_text=f"{service_name} Token",
        env_file_path=env_file_path,
        env_key=env_key,
        placeholders=placeholders,
        is_password=True,
    )


def mint_chronicle_api_key(
    backend_url: str,
    username: str,
    password: str,
    key_name: str,
    timeout: float = 15.0,
) -> Tuple[Optional[str], Optional[str]]:
    """Log in to Chronicle once and exchange the credentials for an API key.

    Client services (relays, sync daemons, dictation apps) need a credential that
    outlives a JWT's 24h lifetime. Rather than storing the account password so
    they can re-login forever, setup uses the password exactly once — here — and
    persists only the resulting long-lived key.

    Args:
        backend_url: Chronicle base URL, e.g. https://host:8000
        username: Chronicle account email
        password: Chronicle account password (not persisted by the caller)
        key_name: Label shown in Settings → API Keys, e.g. "vault-sync (macbook)"

    Returns:
        (api_key, None) on success, or (None, error_message) on failure.
    """
    # Imported here so setup_utils stays importable in environments without
    # requests (it is otherwise dependency-light).
    import requests

    url = backend_url.rstrip("/")
    try:
        login = requests.post(
            f"{url}/auth/jwt/login",
            data={"username": username, "password": password},
            timeout=timeout,
        )
    except requests.exceptions.SSLError:
        return None, (
            f"TLS verification failed for {url} — the server likely uses a "
            "self-signed certificate. Use the plain-HTTP backend port instead, "
            f"e.g. {url.replace('https://', 'http://')}:8000"
        )
    except requests.exceptions.RequestException as e:
        return None, f"Cannot reach the backend at {url}: {e}"

    if login.status_code != 200:
        return (
            None,
            f"Login failed (HTTP {login.status_code}) — check the email/password",
        )
    token = login.json().get("access_token")
    if not token:
        return None, "Login succeeded but returned no access token"

    try:
        created = requests.post(
            f"{url}/api/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": key_name},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        return None, f"Could not create an API key: {e}"

    if created.status_code == 404:
        return None, (
            "This server is too old to issue API keys (no /api/api-keys endpoint). "
            "Update the backend, then re-run setup."
        )
    if created.status_code != 201:
        return None, f"Could not create an API key (HTTP {created.status_code})"

    api_key = created.json().get("token")
    if not api_key:
        return None, "Server created a key but returned no token"
    return api_key, None


def detect_tailscale_info() -> Tuple[Optional[str], Optional[str]]:
    """
    Detect Tailscale DNS name and IPv4 address.

    Returns:
        (dns_name, ip) tuple. dns_name is the MagicDNS hostname (e.g. "myhost.tail1234.ts.net"),
        ip is the Tailscale IPv4 address (e.g. "100.64.1.5").
        Either or both may be None if Tailscale is not available.
    """
    dns_name = None
    ip = None

    # Get MagicDNS name from tailscale status --json
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            raw_dns = status.get("Self", {}).get("DNSName", "")
            # DNSName has trailing dot, strip it
            if raw_dns:
                dns_name = raw_dns.rstrip(".")
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        pass

    # Get IPv4 address as fallback
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return dns_name, ip


def generate_tailscale_certs(certs_dir: str) -> bool:
    """
    Generate trusted TLS certificates via Tailscale.

    Uses `tailscale cert` to obtain certs signed by the Tailscale CA, which are
    automatically trusted on devices in the same tailnet. Tries without sudo first
    (works when the Tailscale operator is set to the current user via
    `tailscale set --operator=$USER`); falls back to `sudo` otherwise.

    Args:
        certs_dir: Directory to write server.crt and server.key into.

    Returns:
        True if certificates were generated successfully, False otherwise.
    """
    dns_name, _ = detect_tailscale_info()
    if not dns_name:
        return False

    certs_path = Path(certs_dir)
    certs_path.mkdir(parents=True, exist_ok=True)

    cert_file = certs_path / "server.crt"
    key_file = certs_path / "server.key"

    cert_cmd = [
        "tailscale",
        "cert",
        "--cert-file",
        str(cert_file),
        "--key-file",
        str(key_file),
        dns_name,
    ]

    try:
        # Try without sudo first (operator configured). Fall back to non-interactive
        # sudo (-n avoids hanging on a password prompt in unattended contexts).
        result = subprocess.run(cert_cmd, capture_output=True, text=True, timeout=30)
        used_sudo = False
        if result.returncode != 0:
            result = subprocess.run(
                ["sudo", "-n", *cert_cmd], capture_output=True, text=True, timeout=30
            )
            used_sudo = True
        if result.returncode != 0:
            return False

        if used_sudo:
            # sudo wrote the files as root; fix ownership so Docker (and our user) can read them
            uid = os.getuid()
            gid = os.getgid()
            subprocess.run(
                ["sudo", "-n", "chown", f"{uid}:{gid}", str(cert_file), str(key_file)],
                capture_output=True,
                timeout=10,
            )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def cert_needs_renewal(certs_dir: str, within_days: int = 21) -> bool:
    """
    Check whether the TLS cert in certs_dir is missing or expiring soon.

    Cheap, local-only check (no network): inspects the cert file's expiry via
    `openssl x509 -checkend`, which exits 0 if the cert is valid beyond the window
    and non-zero if it expires within it (or is already expired).

    Args:
        certs_dir: Directory containing server.crt.
        within_days: Treat the cert as needing renewal if it expires within this many days.

    Returns:
        True if the cert is missing or expires within `within_days`, False otherwise.
    """
    cert_file = Path(certs_dir) / "server.crt"
    if not cert_file.exists():
        return True

    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-checkend",
                str(within_days * 86400),
                "-noout",
                "-in",
                str(cert_file),
            ],
            capture_output=True,
            timeout=10,
        )
        return result.returncode != 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # If expiry can't be determined, err toward renewing.
        return True


def ensure_tailscale_cert(certs_dir: str, within_days: int = 21) -> Optional[bool]:
    """
    Renew the Tailscale TLS cert only if it is missing or near expiry.

    No-op in the common case: just checks the local cert file's expiry and returns
    without contacting Tailscale unless renewal is actually due. This keeps the
    expensive `tailscale cert` call (and Let's Encrypt issuance) to roughly once
    per certificate lifetime regardless of how often it is invoked.

    Args:
        certs_dir: Directory containing/receiving server.crt and server.key.
        within_days: Renew if the cert expires within this many days.

    Returns:
        None if no renewal was needed, True if renewed successfully,
        False if renewal was needed but failed.
    """
    if not cert_needs_renewal(certs_dir, within_days):
        return None
    return generate_tailscale_certs(certs_dir)


def tailscale_socket_path() -> Optional[str]:
    """
    Return the path to the local tailscaled Unix socket if present, else None.

    Used to decide whether Caddy can manage the Tailscale TLS cert itself (socket
    mounted into the container) or whether we must fall back to a host-issued cert
    file (e.g. Docker Desktop on macOS, where the socket isn't reachable from the VM).
    """
    for path in (
        "/var/run/tailscale/tailscaled.sock",
        "/run/tailscale/tailscaled.sock",
    ):
        if Path(path).exists():
            return path
    return None


def tailscaled_enabled_at_boot() -> Optional[bool]:
    """
    Whether the tailscaled systemd unit is enabled to start on boot.

    Returns:
        True  — `systemctl is-enabled tailscaled` reports enabled (it'll come back
                after a reboot).
        False — the unit exists but is disabled/static (started manually only;
                won't survive a reboot — the classic "Tailscale gone after reboot").
        None  — can't tell: not Linux, no systemd/systemctl, or no tailscaled unit
                (e.g. Tailscale.app on macOS, or a non-systemd init). Nothing to offer.
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "tailscaled"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    state = result.stdout.strip()
    if state == "enabled":
        return True
    # "disabled" / "static" → won't auto-start. Unknown unit → systemctl prints
    # nothing to stdout (message goes to stderr, rc=1) → treat as "can't tell".
    if state in ("disabled", "static"):
        return False
    return None


def enable_tailscaled_at_boot() -> bool:
    """
    Enable (and start) the tailscaled unit so it survives reboots.

    Runs `sudo systemctl enable --now tailscaled`. Returns True on success. Uses sudo
    because enabling a system unit needs root; the user may be prompted for a password.
    """
    try:
        return (
            subprocess.run(
                ["sudo", "systemctl", "enable", "--now", "tailscaled"]
            ).returncode
            == 0
        )
    except OSError:
        return False


def decide_cert_mode(server_address: str) -> str:
    """
    Decide how the HTTPS certificate is managed for the given server address.

    Returns:
        "static" — host issues the cert file and Caddy serves it. Only for a Tailscale
            (*.ts.net) address when no tailscaled socket is available to mount into
            Caddy (e.g. Docker Desktop on macOS). Renewed by the services.py startup hook.
        "caddy"  — Caddy obtains and auto-renews the cert itself: *.ts.net via the
            mounted tailscaled socket, a real domain via Let's Encrypt, and an IP or
            localhost via Caddy's internal CA. No host cert file, no renewal cron.
    """
    if server_address.endswith(".ts.net") and not tailscale_socket_path():
        return "static"
    return "caddy"


def detect_cuda_version(default: str = "cu126") -> str:
    """
    Detect system CUDA version from nvidia-smi output.

    Parses "CUDA Version: X.Y" from nvidia-smi and maps to PyTorch CUDA version strings.

    Args:
        default: Default CUDA version if detection fails (default: "cu126")

    Returns:
        PyTorch CUDA version string: "cu126" or "cu128"
    """
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", result.stdout)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                if (major, minor) >= (12, 8):
                    return "cu128"
                # cu126 is the lowest supported build (torch>=2.7 dropped cu121)
                return "cu126"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return default
