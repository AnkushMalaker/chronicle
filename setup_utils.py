"""
Shared utilities for Chronicle setup scripts.

Provides common functions for interactive configuration, password masking,
and environment file handling. Used by wizard.py, init.py scripts, and plugin setup.
"""

import getpass
import json
import re
import secrets
import subprocess
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
    normalized_value = value.replace('-', '_').lower()

    for placeholder in placeholder_variants:
        normalized_placeholder = placeholder.replace('-', '_').lower()
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
        Masked string in format: "first5***********last5"

    Examples:
        >>> mask_value('sk-proj-abc123def456ghi789')
        'sk-pr***************i789'
        >>> mask_value('short')
        'short'
        >>> mask_value('smtp_password_12345')
        'smtp_***********2345'
    """
    # Strip whitespace before processing
    value_clean = value.strip() if value else value

    if not value_clean or len(value_clean) <= show_chars * 2:
        return value

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
    prompt_text: str,
    min_length: int = 8,
    allow_generated: bool = False
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
    env_key: Optional[str] = None
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
    has_valid_existing = existing_value and not is_placeholder(existing_value, *placeholders)

    if has_valid_existing:
        # Show masked value with option to reuse
        if is_password:
            masked = mask_value(existing_value)
            display_prompt = f"{prompt_text} ({masked}) [press Enter to reuse, or enter new]"
        else:
            display_prompt = f"{prompt_text} ({existing_value}) [press Enter to reuse, or enter new]"

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
    placeholders: Optional[List[str]] = None
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
        'your-api-key-here',
        'your_api_key_here',
        f'your-{service_name.lower()}-key-here'
    ]

    return prompt_with_existing_masked(
        prompt_text=f"{service_name} API Key",
        env_file_path=env_file_path,
        env_key=env_key,
        placeholders=placeholders,
        is_password=True
    )


def prompt_token(
    service_name: str,
    env_file_path: str = ".env",
    env_key: Optional[str] = None,
    placeholders: Optional[List[str]] = None
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
        'your-token-here',
        'your_token_here',
        f'your-{service_name.lower()}-token-here'
    ]

    return prompt_with_existing_masked(
        prompt_text=f"{service_name} Token",
        env_file_path=env_file_path,
        env_key=env_key,
        placeholders=placeholders,
        is_password=True
    )


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
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5
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
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return dns_name, ip


def detect_cuda_version(default: str = "cu126") -> str:
    """
    Detect system CUDA version from nvidia-smi output.

    Parses "CUDA Version: X.Y" from nvidia-smi and maps to PyTorch CUDA version strings.

    Args:
        default: Default CUDA version if detection fails (default: "cu126")

    Returns:
        PyTorch CUDA version string: "cu121", "cu126", or "cu128"
    """
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            match = re.search(r'CUDA Version:\s*(\d+)\.(\d+)', result.stdout)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                if (major, minor) >= (12, 8):
                    return "cu128"
                elif (major, minor) >= (12, 6):
                    return "cu126"
                elif (major, minor) >= (12, 1):
                    return "cu121"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return default
