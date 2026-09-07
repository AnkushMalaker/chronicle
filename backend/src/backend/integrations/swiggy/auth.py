"""OAuth provider wiring for pre-authorized, non-interactive workers."""

from __future__ import annotations

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from .tokens import TokenStore

SCOPES = "mcp:tools mcp:resources mcp:prompts"


class NonInteractiveAuthFlow:
    """Refuse browser authorization inside a background worker.

    Existing access/refresh tokens continue to work.  If Swiggy requires human
    authorization, the mode fails explicitly and asks for an out-of-band token
    refresh instead of hanging on stdin or trying to open a browser.
    """

    @property
    def redirect_uri(self) -> str:
        return "http://localhost:8765/callback"

    async def present(self, authorization_url: str) -> None:
        raise RuntimeError(
            "Swiggy authorization is required; refresh the linked token files "
            "outside the Chronicle worker"
        )

    async def await_code(self) -> tuple[str, str | None]:
        raise RuntimeError("Interactive Swiggy authorization is disabled in workers")


def build_oauth_provider(
    server_url: str,
    store: TokenStore,
    *,
    client_name: str = "chronicle-swiggy-mode",
) -> OAuthClientProvider:
    flow = NonInteractiveAuthFlow()
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name=client_name,
            redirect_uris=[flow.redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPES,
            token_endpoint_auth_method="none",
        ),
        storage=store,
        redirect_handler=flow.present,
        callback_handler=flow.await_code,
    )
