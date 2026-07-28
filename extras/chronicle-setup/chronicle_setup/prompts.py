"""Interactive prompts that respect what is already configured.

Every prompt here can be answered by pressing Enter to keep the existing value,
and secrets are shown masked rather than in the clear — setup is re-run often
(new service, changed provider), so the common path must not require retyping
credentials or printing them to a shared terminal.
"""

import getpass
import secrets
from typing import List, Optional

from .env import is_placeholder, mask_value, read_env_value


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
