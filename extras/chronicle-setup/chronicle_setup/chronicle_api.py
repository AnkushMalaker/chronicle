"""Talking to a running Chronicle backend during setup."""

from typing import Optional, Tuple


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
    # Imported here so this module stays importable in environments without
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
