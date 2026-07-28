# chronicle-client

Shared configuration and auth for Chronicle's native client apps.

`chronicle-tray`, `vault-sync`, `local-wearable-client` and `havpe-relay` all need
the same four facts — where the backend is, its WebSocket URL, which API key to
present, and what this device is called — and each used to derive them itself.
This package is the single source.

```python
from chronicle_client import ClientConfig, check_credentials

config = ClientConfig.from_env()
if not check_credentials(config.api_key, config.backend_url):
    ...
```

`ClientConfig.from_env()` loads the repository-root `.env` (the one shared by all
native client components), resolves `BACKEND_URL` through Tailnet discovery when
it isn't set explicitly, and reads `CHRONICLE_API_KEY`.

## Why an API key

A Chronicle JWT expires after 24h. These clients store one credential and never
see a login form again, so a JWT would either break daily or force them to keep
the account password around to re-login. Mint a long-lived key in the web UI
under **Settings → API Keys**.

## Consuming it

```toml
[tool.uv.sources]
chronicle-client = { path = "../chronicle-client", editable = true }
```

For a containerised consumer, pass the package as a second build context so the
main context stays narrow — see `extras/havpe-relay/docker-compose.yml`.
