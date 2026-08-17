"""Private on-disk token storage for one explicitly linked Chronicle user."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class TokenStore(Protocol):
    async def get_tokens(self) -> OAuthToken | None: ...
    async def set_tokens(self, tokens: OAuthToken) -> None: ...
    async def get_client_info(self) -> OAuthClientInformationFull | None: ...
    async def set_client_info(self, info: OAuthClientInformationFull) -> None: ...


class MemoryTokenStore:
    def __init__(
        self,
        tokens: OAuthToken | None = None,
        client_info: OAuthClientInformationFull | None = None,
    ):
        self._tokens = tokens
        self._client_info = client_info

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._client_info = info


class FileTokenStore:
    """Crash-safe credential files restricted to the process owner."""

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        self._tokens = self._dir / "tokens.json"
        self._client = self._dir / "client.json"
        for credential_path in (self._tokens, self._client):
            if credential_path.exists():
                credential_path.chmod(0o600)

    @property
    def configured(self) -> bool:
        return self._tokens.is_file() and self._client.is_file()

    @staticmethod
    def _read(path: Path, model):
        if not path.exists():
            return None
        return model.model_validate_json(path.read_text())

    @staticmethod
    def _write(path: Path, value) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value.model_dump_json(indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)

    async def get_tokens(self) -> OAuthToken | None:
        return self._read(self._tokens, OAuthToken)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write(self._tokens, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._read(self._client, OAuthClientInformationFull)

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._write(self._client, info)
