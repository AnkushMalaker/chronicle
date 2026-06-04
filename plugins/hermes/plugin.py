"""
Hermes Agent plugin for Chronicle.

Listens for the "hermes" keyword in transcripts and forwards whatever the user
said (with the keyword stripped) to an external Hermes agent via its
OpenAI-compatible HTTP API (POST /v1/chat/completions). The agent's reply is
returned as the plugin result message, which Chronicle logs and records in the
plugin event log.

Conversation continuity: each request carries an ``X-Hermes-Session-Id`` header
keyed by the Chronicle conversation id, so all utterances within one
conversation share the same Hermes-side session/context.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


def _normalize_base_url(api_url: str) -> str:
    """Normalize a user-provided Hermes URL to a scheme://host:port base.

    Strips a trailing slash and a trailing ``/v1`` so the caller can append
    ``/v1/<endpoint>`` exactly once regardless of how the user typed it.

    Examples:
        "http://hermes.ts.net:8642/"   -> "http://hermes.ts.net:8642"
        "http://hermes.ts.net:8642/v1" -> "http://hermes.ts.net:8642"
    """
    url = (api_url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


class HermesPlugin(BasePlugin):
    """
    Forward keyword-triggered voice commands to an external Hermes agent.

    Example:
        User says: "Hermes, what's on my calendar tomorrow?"
        -> Router detects keyword "hermes", strips it, passes command to on_transcript()
        -> Plugin POSTs the command to Hermes /v1/chat/completions
        -> Returns PluginResult with Hermes's reply as the message
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ["transcript"]

    name = "Hermes Agent"
    description = 'Routes "hermes …" voice commands to an external Hermes agent over its OpenAI-compatible API'

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Hermes plugin.

        Args:
            config: Plugin configuration with keys:
                - api_url: Hermes server base URL (scheme://host:port, e.g. a
                  Tailscale MagicDNS name or LAN IP). ``/v1`` is optional.
                - model: Model name advertised by the Hermes server (default "hermes")
                - timeout: Request timeout in seconds (default 120)
                - system_prompt: System prompt sent with each request
                - api_key: Optional bearer token (from HERMES_API_KEY in .env)
        """
        super().__init__(config)
        self.api_url = _normalize_base_url(
            config.get("api_url", "http://localhost:8642")
        )
        self.model = config.get("model", "hermes")
        self.timeout = int(config.get("timeout", 120))
        # Hermes has its own core system prompt; anything here is layered on top.
        # Empty by default so Hermes's own identity is used unchanged.
        self.system_prompt = (config.get("system_prompt") or "").strip()

        # api_key comes from ${HERMES_API_KEY}; if the env var is unset the
        # placeholder survives expansion, so treat that as "no key".
        api_key = config.get("api_key", "") or ""
        self.api_key = "" if "${" in api_key else api_key.strip()

        # Optional: also push the reply to Discord via the webhook "notify" route
        # (deliver_only — no extra LLM cost). Disabled if either is unset.
        self.webhook_url = (config.get("webhook_url") or "").strip().rstrip("/")
        webhook_secret = config.get("webhook_secret", "") or ""
        self.webhook_secret = "" if "${" in webhook_secret else webhook_secret.strip()

        self._client: Optional[httpx.AsyncClient] = None

    async def _push_to_discord(self, text: str) -> None:
        """Best-effort push of text to the Hermes webhook "notify" route.

        Fire-and-forget: failures are logged but never affect the reply that
        Chronicle already captured from the API server. Skipped if the webhook
        URL or secret is not configured.
        """
        if not (self.webhook_url and self.webhook_secret) or self._client is None:
            return
        try:
            body = json.dumps({"text": text}).encode()
            signature = (
                "sha256="
                + hmac.new(
                    self.webhook_secret.encode(), body, hashlib.sha256
                ).hexdigest()
            )
            resp = await self._client.post(
                self.webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
            resp.raise_for_status()
            logger.info("Pushed Hermes reply to Discord webhook")
        except Exception as e:
            logger.warning(f"Failed to push Hermes reply to Discord webhook: {e}")

    def _headers(self, session_id: Optional[str] = None) -> Dict[str, str]:
        """Build request headers, including optional bearer auth and session id."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    async def initialize(self):
        """Create the shared HTTP client. Raises if no api_url is configured."""
        if not self.enabled:
            logger.info("Hermes plugin is disabled, skipping initialization")
            return

        if not self.api_url:
            raise ValueError(
                "Hermes api_url is required (e.g. http://hermes-host:8642)"
            )

        logger.info(
            f"Initializing Hermes plugin (URL: {self.api_url}, model: {self.model})"
        )
        self._client = httpx.AsyncClient(timeout=self.timeout)

        # Best-effort connectivity check against the unauthenticated /health endpoint.
        try:
            resp = await self._client.get(f"{self.api_url}/health")
            resp.raise_for_status()
            logger.info("Hermes plugin initialized successfully")
        except Exception as e:
            # Don't hard-fail init: the RPi server may be momentarily down. Log
            # loudly so the failure is visible, but let the plugin load so it can
            # recover on the next utterance.
            logger.warning(
                f"Hermes health check failed during init ({self.api_url}): {e}"
            )

    async def cleanup(self):
        """Close the shared HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> Dict[str, Any]:
        """Live connectivity check against the Hermes /health endpoint."""
        if self._client is None:
            return {"ok": False, "message": "HTTP client not initialized"}
        try:
            start = time.time()
            resp = await self._client.get(f"{self.api_url}/health")
            latency_ms = int((time.time() - start) * 1000)
            resp.raise_for_status()
            return {"ok": True, "message": "Hermes reachable", "latency_ms": latency_ms}
        except Exception as e:
            return {"ok": False, "message": f"Hermes unreachable: {e}"}

    async def _dispatch_command(
        self,
        command: str,
        conversation_id: Optional[str],
        empty_message: str,
    ) -> PluginResult:
        """Forward a resolved command to the Hermes agent and return its reply.

        Shared by both trigger paths so the acoustic wake word and the text
        keyword reach the agent identically:
        - ``on_transcript`` (text keyword, ``keyword_anywhere: [hermes]``)
        - ``on_wake_word_detected`` (acoustic, from the wakeword-service)

        Args:
            command: The command text (already stripped of any keyword).
            conversation_id: Conversation id used as the Hermes session id.
            empty_message: Message returned when ``command`` is empty.
        """
        command = (command or "").strip()

        if not command:
            return PluginResult(
                success=False,
                message=empty_message,
                should_continue=True,
            )

        if self._client is None:
            logger.error("Hermes HTTP client not initialized")
            return PluginResult(
                success=False,
                message="Sorry, Hermes is not connected.",
                should_continue=True,
            )

        # Hermes applies its own core system prompt; only send one if the user
        # configured extra context to layer on top.
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": command})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        try:
            logger.info(f"Forwarding command to Hermes: '{command}'")
            resp = await self._client.post(
                f"{self.api_url}/v1/chat/completions",
                json=payload,
                headers=self._headers(session_id=conversation_id),
            )
            resp.raise_for_status()
            data = resp.json()

            reply = data["choices"][0]["message"]["content"].strip()
            logger.info(f"Hermes replied ({len(reply)} chars)")

            # Best-effort push to Discord (#hermes) via the notify webhook.
            # Quote the spoken request (Discord blockquote) above the reply so
            # the channel has context. Each line is prefixed for multi-line input.
            quoted = "\n".join(f"> {line}" for line in command.splitlines())
            discord_text = f"{quoted}\n\n{reply}" if quoted else reply
            await self._push_to_discord(discord_text)

            return PluginResult(
                success=True,
                message=reply,
                data={
                    "command": command,
                    "reply": reply,
                    "conversation_id": conversation_id,
                    "model": data.get("model", self.model),
                },
                # The utterance was directed at Hermes; consume it so other
                # transcript plugins don't also act on it.
                should_continue=False,
            )

        except httpx.HTTPStatusError as e:
            body = e.response.text[:300] if e.response is not None else ""
            logger.error(f"Hermes returned HTTP {e.response.status_code}: {body}")
            return PluginResult(
                success=False,
                message=f"Hermes returned an error (HTTP {e.response.status_code}).",
                should_continue=True,
            )
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"Unexpected Hermes response shape: {e}")
            return PluginResult(
                success=False,
                message="Hermes returned an unexpected response.",
                should_continue=True,
            )
        except Exception as e:
            logger.error(f"Failed to reach Hermes: {e}")
            return PluginResult(
                success=False,
                message=f"Couldn't reach Hermes: {e}",
                should_continue=True,
            )

    async def on_transcript(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Forward a keyword-triggered command to Hermes and return its reply.

        The router has already detected the "hermes" keyword and stripped it,
        placing the remaining text in ``context.data["command"]``.
        """
        return await self._dispatch_command(
            command=context.data.get("command"),
            conversation_id=context.data.get("conversation_id"),
            empty_message="I heard the Hermes keyword but no command followed it.",
        )

    async def on_wake_word_detected(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Forward an acoustically-triggered command to Hermes.

        The standalone wakeword-service detected the acoustic "Hermes" wake word,
        captured the following turn, and resolved its text from the existing
        transcription — placed in ``context.data["command"]``. This shares the
        exact agent-call path as the text keyword trigger.
        """
        return await self._dispatch_command(
            command=context.data.get("command"),
            conversation_id=context.data.get("conversation_id"),
            empty_message="I heard the Hermes wake word but couldn't make out the command.",
        )

    @staticmethod
    async def test_connection(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate Hermes connectivity (and auth, if configured) without invoking
        the agent. Used by the form-based configuration UI.

        Hits ``GET /v1/models`` with the configured bearer token. This confirms
        both reachability and that the API key (if any) is accepted, while being
        far cheaper than a full chat completion.

        Args:
            config: Flat config dict from the test UI. Settings keep their
                original keys (api_url, model); env vars arrive lowercased
                (hermes_api_key).

        Returns:
            Dict with success status, message, and optional details.
        """
        api_url = _normalize_base_url(config.get("api_url", ""))
        if not api_url:
            return {
                "success": False,
                "message": "api_url is required (e.g. http://hermes-host:8642)",
                "status": "error",
            }

        # Settings expose api_key as-is; the env-var path lowercases to hermes_api_key.
        api_key = config.get("api_key") or config.get("hermes_api_key") or ""
        if "${" in api_key:
            api_key = ""

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            start = time.time()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{api_url}/v1/models", headers=headers)
            latency_ms = int((time.time() - start) * 1000)

            if resp.status_code in (401, 403):
                return {
                    "success": False,
                    "message": "Hermes rejected the API key (check HERMES_API_KEY).",
                    "status": "error",
                }
            resp.raise_for_status()

            return {
                "success": True,
                "message": f"Connected to Hermes at {api_url} ({latency_ms}ms)",
                "status": "ok",
                "latency_ms": latency_ms,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Could not reach Hermes at {api_url}: {e}",
                "status": "error",
            }
