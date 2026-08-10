"""Pi CLI executor for Chronicle's memory write and retrieval agents.

Pi supplies the agent loop, but it never receives filesystem or shell tools.  A generated
JavaScript extension registers Chronicle's canonical vault schemas and forwards each call
to a short-lived, bearer-authenticated HTTP server bound to loopback.  The Python side then
dispatches through :class:`VaultTools`, preserving the same validation, locking, and audit
tracking as the direct executor.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import unquote, urlsplit

from advanced_omi_backend.model_registry import (
    AppModels,
    ResolvedLLMOperation,
    get_models_registry,
)

from ..telemetry import (
    current_memory_attempt,
    current_otel_context,
    memory_span,
    record_llm_usage_span,
    set_observation_io,
    set_safe_span_attributes,
    text_payload,
)
from .memory_agent import (
    AGENT_SYSTEM_PROMPT_ID,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    MAX_SEARCH_ROUNDS,
    MAX_TOOL_ROUNDS,
    PI_SEARCH_FAILURE_ANSWER,
    SEARCH_FINAL_SYNTHESIS_SYSTEM_SUFFIX,
    SEARCH_SYSTEM_PROMPT,
    MemoryAgentResult,
    VaultSearchResult,
    _get_prompt,
    _search_final_synthesis_prompt,
    build_write_task,
    forbidden_folders,
    required_notes,
)
from .vault_skill import write_skill
from .vault_tools import (
    VAULT_SEARCH_TOOL_SCHEMAS,
    VAULT_TOOL_SCHEMAS,
    VaultToolError,
    VaultTools,
)

logger = logging.getLogger("memory_service.agent.pi")

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_CONTEXT_WINDOW = 32768
DEFAULT_MAX_TOKENS = 4096
MIN_PROMPT_HEADROOM_TOKENS = 1024
MAX_PI_WRITE_TOOL_CALLS = MAX_TOOL_ROUNDS * 4
_PI_TOOL_CALLS_PER_SEARCH_ROUND = 4
_STDERR_TAIL_CHARS = 2000
_MAX_GATEWAY_REQUEST_BYTES = 16 * 1024 * 1024
_GATEWAY_DRAIN_TIMEOUT_SECONDS = 65.0
_ALL_VAULT_TOOLS = frozenset(
    schema["function"]["name"] for schema in VAULT_TOOL_SCHEMAS
)
_READ_ONLY_VAULT_TOOLS = frozenset(
    schema["function"]["name"] for schema in VAULT_SEARCH_TOOL_SCHEMAS
)
if not _READ_ONLY_VAULT_TOOLS < _ALL_VAULT_TOOLS:
    raise RuntimeError(
        "Pi vault schemas must define a nonempty write-only tool difference"
    )
# verify_vault only reads; it is absent from the search agent's set because a read-only
# retrieval run has nothing to verify, not because it mutates.
_MUTATING_VAULT_TOOLS = _ALL_VAULT_TOOLS - _READ_ONLY_VAULT_TOOLS - {"verify_vault"}

# Pi keeps `--no-builtin-tools`. Its native `read` looked like the obvious fix for our
# unbounded read_note, but in the pinned 0.83.0 `resolveReadPathAsync` only resolves the
# path against cwd — absolute paths pass straight through and there is no confinement
# check (upstream added one in a later release). The memory agent reads untrusted
# transcript content, so granting it would let an injected transcript read /codex-home,
# .env, or another user's vault and write the contents into a synced note. read_note is
# bounded instead; see vault_tools.read_note.
_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_PROXY_ENV_NAMES = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
}
_PI_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_PI_COMPAT_BOOLEAN_FIELDS = {
    "supportsDeveloperRole",
    "supportsReasoningEffort",
    "supportsStore",
    "supportsStrictMode",
    "supportsUsageInStreaming",
}
_PI_MAX_TOKEN_FIELDS = {"max_completion_tokens", "max_tokens"}
_PI_THINKING_FORMATS = {
    "ant-ling",
    "chat-template",
    "deepseek",
    "openai",
    "openrouter",
    "qwen",
    "qwen-chat-template",
    "string-thinking",
    "together",
    "zai",
}


class PiExecutorError(RuntimeError):
    """A Pi configuration, startup, or pre-execution validation failure."""


class _PiLimitExceeded(RuntimeError):
    """A gateway preflight rejected a tool call beyond the configured hard cap."""


@dataclass(frozen=True)
class _PiRuntimeConfig:
    binary: str
    provider: str
    model: str
    base_url: str
    api_key: str
    thinking: str
    max_tokens: int
    context_window: int
    timeout_seconds: int
    reasoning: bool
    temperature: float
    input_modalities: List[str] = field(default_factory=lambda: ["text"])
    system_prompt_prefix: str = ""
    compat: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _PiEventResult:
    summary: str = ""
    rounds: int = 0
    tool_calls: int = 0
    usage: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    fatal_errors: List[str] = field(default_factory=list)
    truncated: bool = False
    agent_ended: bool = False


def _pi_settings(registry: AppModels) -> Dict[str, Any]:
    """Return exactly ``memory.backends.pi`` from the merged Chronicle config."""
    memory = registry.memory or {}
    backends = memory.get("backends")
    if backends is None:
        backends = {}
    if not isinstance(backends, dict):
        raise PiExecutorError("memory.backends must be a mapping")
    settings = backends.get("pi")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise PiExecutorError("memory.backends.pi must be a mapping")
    return dict(settings)


def _positive_int(value: Any, *, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise PiExecutorError(f"memory.backends.pi.{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PiExecutorError(
            f"memory.backends.pi.{name} must be a positive integer"
        ) from exc
    if parsed <= 0:
        raise PiExecutorError(f"memory.backends.pi.{name} must be a positive integer")
    return parsed


def _provider_slug(provider: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-") or "llm"
    return f"chronicle-{suffix}"


def _thinking_level(value: Any, operation: ResolvedLLMOperation) -> str:
    if value is None or value == "":
        value = operation.reasoning_effort
    if value is None or value == "":
        value = "low" if operation.model_def.thinking else "off"
    if isinstance(value, bool):
        value = "low" if value else "off"
    level = str(value).strip().lower()
    if level in ("none", "0"):
        level = "off"
    if level not in _THINKING_LEVELS:
        allowed = ", ".join(sorted(_THINKING_LEVELS))
        raise PiExecutorError(
            f"memory.backends.pi.thinking must be one of {allowed}; got {value!r}"
        )
    return level


def _validated_pi_compat(value: Any, *, source: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PiExecutorError(f"{source} must be a mapping")

    allowed = _PI_COMPAT_BOOLEAN_FIELDS | {"maxTokensField", "thinkingFormat"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PiExecutorError(
            f"{source} contains unsupported field(s): {', '.join(unknown)}"
        )

    compat = dict(value)
    for name in _PI_COMPAT_BOOLEAN_FIELDS:
        if name in compat and not isinstance(compat[name], bool):
            raise PiExecutorError(f"{source}.{name} must be a boolean")
    if (
        "maxTokensField" in compat
        and compat["maxTokensField"] not in _PI_MAX_TOKEN_FIELDS
    ):
        allowed_tokens = ", ".join(sorted(_PI_MAX_TOKEN_FIELDS))
        raise PiExecutorError(
            f"{source}.maxTokensField must be one of {allowed_tokens}"
        )
    if (
        "thinkingFormat" in compat
        and compat["thinkingFormat"] not in _PI_THINKING_FORMATS
    ):
        allowed_formats = ", ".join(sorted(_PI_THINKING_FORMATS))
        raise PiExecutorError(
            f"{source}.thinkingFormat must be one of {allowed_formats}"
        )
    return compat


def _pi_model_compat(
    model_def: Any, resolved: ResolvedLLMOperation, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Derive conservative OpenAI-completions compatibility, then apply overrides."""
    provider = str(model_def.model_provider).strip().lower()
    model = str(resolved.model_name).strip().lower()
    local_provider = any(
        marker in provider for marker in ("llama.cpp", "llamacpp", "ollama", "local")
    )
    official_openai = provider == "openai"

    compat: Dict[str, Any] = {
        "supportsDeveloperRole": official_openai and not local_provider,
        "supportsReasoningEffort": official_openai and not local_provider,
        "maxTokensField": (
            "max_completion_tokens"
            if official_openai
            and (model.startswith("gpt-5") or re.match(r"o\d", model))
            else "max_tokens"
        ),
    }
    if bool(model_def.thinking):
        if "qwen" in model:
            compat["thinkingFormat"] = "qwen-chat-template"
        elif "deepseek" in model:
            compat["thinkingFormat"] = "deepseek"
        elif "glm" in model or "zai" in provider:
            compat["thinkingFormat"] = "zai"

    model_params = model_def.model_params or {}
    compat.update(
        _validated_pi_compat(
            model_params.get("pi_compat"),
            source=f"models.{model_def.name}.model_params.pi_compat",
        )
    )
    compat.update(
        _validated_pi_compat(settings.get("compat"), source="memory.backends.pi.compat")
    )
    return compat


def _resolve_pi_config(
    operation: str, *, force_fallback: bool = False
) -> _PiRuntimeConfig:
    registry = get_models_registry()
    if registry is None:
        raise PiExecutorError("Chronicle model registry is unavailable")
    settings = _pi_settings(registry)

    configured_model = str(settings.get("model") or "").strip() or None
    resolved = registry.get_llm_operation(operation, model_override=configured_model)
    if force_fallback:
        fallback = registry.get_fallback_llm_operation(operation, primary=resolved)
        if fallback is None:
            raise PiExecutorError(
                f"no distinct fallback LLM is configured for operation {operation!r}"
            )
        resolved = fallback

    model_def = resolved.model_def
    if str(model_def.api_family).lower() != "openai":
        raise PiExecutorError(
            f"Pi backend requires an OpenAI-compatible LLM; "
            f"model {model_def.name!r} uses api_family={model_def.api_family!r}"
        )
    base_url = resolved.base_url.strip()
    if not base_url:
        raise PiExecutorError(f"model {model_def.name!r} has no resolvable base URL")

    model_params = model_def.model_params or {}
    prefix = model_def.system_prompt_prefix
    capabilities = {
        str(item).strip().lower() for item in (model_def.capabilities or [])
    }
    input_modalities = ["text", "image"] if "vision" in capabilities else ["text"]
    context_default = getattr(model_def, "context_window", None)
    if context_default is None:
        context_default = model_params.get("context_window")
    context_window = _positive_int(
        settings.get("context_window", context_default),
        name="context_window",
        default=DEFAULT_CONTEXT_WINDOW,
    )
    max_tokens = _positive_int(
        settings.get("max_tokens", resolved.max_tokens),
        name="max_tokens",
        default=DEFAULT_MAX_TOKENS,
    )
    if max_tokens > context_window - MIN_PROMPT_HEADROOM_TOKENS:
        raise PiExecutorError(
            "memory.backends.pi.max_tokens must leave at least "
            f"{MIN_PROMPT_HEADROOM_TOKENS} tokens of context for the prompt"
        )

    available, binary = pi_executor_available()
    if not available:
        raise PiExecutorError(binary)

    return _PiRuntimeConfig(
        binary=binary,
        provider=_provider_slug(str(model_def.model_provider)),
        model=resolved.model_name,
        base_url=base_url,
        api_key=resolved.api_key or "no-key",
        thinking=_thinking_level(settings.get("thinking"), resolved),
        max_tokens=max_tokens,
        context_window=context_window,
        timeout_seconds=_positive_int(
            settings.get("timeout_seconds"),
            name="timeout_seconds",
            default=DEFAULT_TIMEOUT_SECONDS,
        ),
        reasoning=bool(model_def.thinking),
        temperature=resolved.temperature,
        input_modalities=input_modalities,
        system_prompt_prefix=prefix.strip(),
        compat=_pi_model_compat(model_def, resolved, settings),
    )


def validate_pi_executor_config(
    operation: str, *, force_fallback: bool = False
) -> None:
    """Validate that one operation resolves to a runnable Pi configuration."""
    _resolve_pi_config(operation, force_fallback=force_fallback)


def pi_executor_available() -> tuple[bool, str]:
    """Return whether the Pi executable is available; Pi needs no separate auth file."""
    requested = os.environ.get("PI_BINARY", "pi")
    binary = shutil.which(requested)
    if not binary:
        return False, f"Pi binary {requested!r} not found on PATH"
    return True, binary


class _VaultToolGateway:
    """Short-lived loopback HTTP adapter around one canonical ``VaultTools`` instance."""

    def __init__(
        self,
        vault_root: Path,
        schemas: Sequence[Dict[str, Any]],
        *,
        max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
        on_limit: Optional[Callable[[], None]] = None,
        required_notes: Sequence[str] = (),
        forbidden_folders: Sequence[str] = (),
        user_id: str = "",
    ):
        self.tools = VaultTools(
            vault_root,
            trace_context=current_otel_context(),
            required_notes=required_notes,
            forbidden_folders=forbidden_folders,
            user_id=user_id,
        )
        self.schemas = list(schemas)
        self.allowed_names = {
            str(schema.get("function", {}).get("name", "")) for schema in self.schemas
        }
        self.token = secrets.token_urlsafe(32)
        self.errors: List[str] = []
        self.read_notes: Dict[str, str] = {}
        self.call_count = 0
        self.max_tool_calls = max_tool_calls
        self.limit_error: Optional[str] = None
        self.close_error: Optional[str] = None
        # ``_closed`` fences admission immediately. ``_state_frozen`` is delayed
        # until admitted handlers drain (or the bounded drain expires), allowing
        # their completed read/error audit to reach the returned result without
        # permitting a late handler to race result assembly.
        self._closed = False
        self._state_frozen = False
        self._on_limit = on_limit
        self._state_lock = threading.Lock()
        self._request_condition = threading.Condition()
        self._active_requests = 0
        self._close_lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Pi vault-tool gateway has not been started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/tool"

    def __enter__(self) -> "_VaultToolGateway":
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path not in {"/limit", "/tool"}:
                    self._send_json(404, {"error": "not found"})
                    return
                authorization = self.headers.get("Authorization") or ""
                if not secrets.compare_digest(authorization, f"Bearer {gateway.token}"):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "invalid Content-Length"})
                    return
                if length <= 0 or length > _MAX_GATEWAY_REQUEST_BYTES:
                    self._send_json(413, {"error": "invalid request size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"error": "request body must be JSON"})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400, {"error": "request body must be an object"})
                    return
                if self.path == "/limit":
                    reason = payload.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        self._send_json(400, {"error": "limit reason must be a string"})
                        return
                    # Authentication and parsing completed inside an accepted
                    # request, so retain this hard-limit outcome if close races it.
                    gateway.set_limit(reason.strip(), admitted=True)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                name = payload.get("name")
                arguments = payload.get("arguments", {})
                if not isinstance(name, str) or name not in gateway.allowed_names:
                    self._send_json(400, {"error": f"unknown tool: {name}"})
                    return
                if not isinstance(arguments, dict):
                    self._send_json(400, {"error": "tool arguments must be an object"})
                    return
                try:
                    result = gateway.dispatch(name, arguments)
                except _PiLimitExceeded as exc:
                    self._send_json(429, {"error": str(exc)})
                    return
                self._send_json(200, {"result": result})

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    logger.debug("Pi disconnected before gateway response was written")

        class GatewayServer(ThreadingHTTPServer):
            # Track accepted work ourselves so close can drain it with a hard deadline.
            daemon_threads = True
            block_on_close = False

            def process_request(self, request: Any, client_address: Any) -> None:
                gateway._request_started()
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    gateway._request_finished()
                    raise

            def process_request_thread(self, request: Any, client_address: Any) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    gateway._request_finished()

        self._server = GatewayServer(("127.0.0.1", 0), Handler)
        server = self._server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="chronicle-pi-vault-tools",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self._close()

    def _request_started(self) -> None:
        with self._request_condition:
            self._active_requests += 1

    def _request_finished(self) -> None:
        with self._request_condition:
            self._active_requests -= 1
            self._request_condition.notify_all()

    def _record_close_error(self, error: str) -> None:
        with self._state_lock:
            if self.close_error is None:
                self.close_error = error
                self.errors.append(error)

    def _close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                self._closed = True

            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            if server is not None:
                server.shutdown()
                server.server_close()

            # Mutating calls serialize through this gate and recheck ``_closed``
            # after acquiring it. Waiting here lets the one canonical mutation that
            # already started finish (including its VaultTools audit updates); queued
            # calls can only acquire the gate afterward and are rejected. _close runs
            # in asyncio.to_thread(), so even a slow finite vault operation never
            # blocks the event loop.
            with self._mutation_lock:
                pass

            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    self._record_close_error(
                        "Pi vault gateway server did not stop within 5 seconds"
                    )

            deadline = time.monotonic() + _GATEWAY_DRAIN_TIMEOUT_SECONDS
            with self._request_condition:
                while self._active_requests:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._record_close_error(
                            "Pi vault gateway requests did not drain within "
                            f"{_GATEWAY_DRAIN_TIMEOUT_SECONDS:g} seconds; "
                            "vault mutations are complete, but read/error details "
                            "may be omitted"
                        )
                        break
                    self._request_condition.wait(timeout=remaining)

            with self._state_lock:
                self._state_frozen = True

    async def aclose(self) -> None:
        """Stop and drain the HTTP server without blocking the asyncio event loop."""
        await asyncio.to_thread(self._close)

    def set_limit(self, reason: str, *, admitted: bool = False) -> None:
        notify = False
        with self._state_lock:
            if (
                not self._state_frozen
                and (admitted or not self._closed)
                and self.limit_error is None
            ):
                self.limit_error = reason
                notify = True
        if notify and self._on_limit is not None:
            self._on_limit()

    def dispatch(self, name: str, arguments: Dict[str, Any]) -> str:
        limit_error: Optional[str] = None
        with self._state_lock:
            if self._closed:
                return "Error: Pi vault gateway is closing"
            if self.call_count >= self.max_tool_calls:
                limit_error = (
                    f"Pi tool-call limit exceeded ({self.max_tool_calls}); "
                    "the extra vault mutation was rejected"
                )
            else:
                # Authorized calls count even if VaultTools rejects their arguments.
                self.call_count += 1
        if limit_error is not None:
            # This call passed the admission check above, so preserve its limit
            # outcome if shutdown starts between releasing the lock and recording it.
            self.set_limit(limit_error, admitted=True)
            raise _PiLimitExceeded(limit_error)
        if name in _MUTATING_VAULT_TOOLS:
            return self._dispatch_serialized_mutation(name, arguments)
        return self._execute_tool(name, arguments)

    def _execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """Dispatch one tool and preserve its result metadata when still relevant."""
        try:
            result = self.tools.dispatch(name, arguments)
        except VaultToolError as exc:
            error = f"{name}: {exc}"
            with self._state_lock:
                if not self._state_frozen:
                    self.errors.append(error)
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface unexpected tool failures
            error = f"{name}: {type(exc).__name__}: {exc}"
            with self._state_lock:
                if not self._state_frozen:
                    self.errors.append(error)
            logger.exception("Pi vault tool %s crashed", name)
            return f"Error: {type(exc).__name__}: {exc}"

        if name == "read_note":
            path = str(arguments.get("path", "?"))
            with self._state_lock:
                if not self._state_frozen:
                    self.read_notes[path] = result
        return result

    def _dispatch_serialized_mutation(
        self, name: str, arguments: Dict[str, Any]
    ) -> str:
        """Run one canonical mutation, rejecting calls queued behind shutdown."""
        with self._mutation_lock:
            with self._state_lock:
                if self._closed:
                    return "Error: Pi vault gateway is closing"
            # Keep error capture inside the mutation gate too. Shutdown therefore
            # cannot observe a completed mutation without its corresponding audit
            # error, even when it marked the gateway closed while the tool ran.
            return self._execute_tool(name, arguments)


def _extension_source(
    schemas: Sequence[Dict[str, Any]],
    *,
    gateway_url: str,
    token: str,
    temperature: float = 0.2,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
) -> str:
    """Generate a dependency-free ESM extension exposing only canonical schemas."""
    tool_defs = [schema["function"] for schema in schemas]
    return f"""\
const gatewayUrl = {json.dumps(gateway_url)};
const limitUrl = {json.dumps(gateway_url.removesuffix("/tool") + "/limit")};
const bearerToken = {json.dumps(token)};
const toolDefinitions = {json.dumps(tool_defs, separators=(",", ":"))};
const temperature = {json.dumps(temperature)};
const maxToolRounds = {max_tool_rounds};
const maxToolCalls = {max_tool_calls};

export default function (pi) {{
  let toolRounds = 0;
  let toolCalls = 0;
  let roundHasTool = false;
  let limitReported = false;

  pi.on("before_provider_request", (event) => ({{
    ...event.payload,
    temperature,
  }}));

  pi.on("turn_start", () => {{
    roundHasTool = false;
  }});

  async function stopAtLimit(reason, context) {{
    if (!limitReported) {{
      limitReported = true;
      try {{
        await fetch(limitUrl, {{
          method: "POST",
          headers: {{
            "Authorization": `Bearer ${{bearerToken}}`,
            "Content-Type": "application/json",
          }},
          body: JSON.stringify({{ reason }}),
        }});
      }} catch (_error) {{
        // The blocking result still prevents the tool call if the process-level
        // watcher cannot be notified because the gateway is already closing.
      }}
    }}
    context.abort();
    return {{ block: true, reason }};
  }}

  pi.on("tool_call", async (_event, context) => {{
    const nextToolRound = toolRounds + (roundHasTool ? 0 : 1);
    if (nextToolRound > maxToolRounds) {{
      return stopAtLimit(`Pi tool-round limit exceeded (${{maxToolRounds}})`, context);
    }}
    const nextToolCall = toolCalls + 1;
    if (nextToolCall > maxToolCalls) {{
      return stopAtLimit(`Pi tool-call limit exceeded (${{maxToolCalls}})`, context);
    }}
    toolRounds = nextToolRound;
    toolCalls = nextToolCall;
    roundHasTool = true;
  }});

  for (const definition of toolDefinitions) {{
    pi.registerTool({{
      name: definition.name,
      label: definition.name,
      description: definition.description,
      parameters: definition.parameters,
      executionMode: "sequential",
      async execute(_toolCallId, params, signal) {{
        const response = await fetch(gatewayUrl, {{
          method: "POST",
          headers: {{
            "Authorization": `Bearer ${{bearerToken}}`,
            "Content-Type": "application/json",
          }},
          body: JSON.stringify({{ name: definition.name, arguments: params }}),
          signal,
        }});
        const body = await response.text();
        if (!response.ok) {{
          throw new Error(`Chronicle vault gateway ${{response.status}}: ${{body}}`);
        }}
        let payload;
        try {{
          payload = JSON.parse(body);
        }} catch (error) {{
          throw new Error(`Chronicle vault gateway returned invalid JSON: ${{error}}`);
        }}
        if (typeof payload.result !== "string") {{
          throw new Error("Chronicle vault gateway response omitted string result");
        }}
        return {{
          content: [{{ type: "text", text: payload.result }}],
          details: {{}},
        }};
      }},
    }});
  }}
}}
"""


def _models_payload(config: _PiRuntimeConfig) -> Dict[str, Any]:
    return {
        "providers": {
            config.provider: {
                "baseUrl": config.base_url,
                "api": "openai-completions",
                # Pi resolves leading ! as a shell command and $NAME as an env
                # reference. Chronicle supplies a literal key, so escape Pi's value
                # syntax before writing the isolated model registry.
                "apiKey": _pi_literal_config_value(config.api_key),
                "compat": config.compat,
                "models": [
                    {
                        "id": config.model,
                        "name": config.model,
                        "reasoning": config.reasoning,
                        "input": config.input_modalities,
                        "contextWindow": config.context_window,
                        "maxTokens": config.max_tokens,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }


def _pi_literal_config_value(value: str) -> str:
    """Encode one literal for Pi's models.json config-value resolver."""
    escaped = value.replace("$", "$$")
    return f"${escaped}" if escaped.startswith("!") else escaped


def _pi_subprocess_env(temp_dir: Path) -> Dict[str, str]:
    """Pass only networking/runtime essentials, never the backend's secret-filled env."""
    env = {
        name: value for name, value in os.environ.items() if name in _PI_ENV_ALLOWLIST
    }
    # Pi's HTTP stack installs an environment-proxy dispatcher globally. Without an
    # explicit bypass, its generated vault extension can send the loopback gateway's
    # bearer token, tool arguments, and note content through HTTP(S)_PROXY. Merge both
    # conventional spellings because proxy libraries disagree about case.
    no_proxy_entries: List[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        for entry in env.get(name, "").split(","):
            normalized = entry.strip()
            if normalized and normalized not in no_proxy_entries:
                no_proxy_entries.append(normalized)
    for loopback in ("127.0.0.1", "localhost", "::1"):
        if loopback not in no_proxy_entries:
            no_proxy_entries.append(loopback)
    no_proxy = ",".join(no_proxy_entries)
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(temp_dir),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    return env


def _redact(value: str, *secrets_to_remove: str) -> str:
    redacted = value
    secrets_by_length = sorted(
        {secret for secret in secrets_to_remove if secret and secret != "no-key"},
        key=len,
        reverse=True,
    )
    for secret in secrets_by_length:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _url_credential_values(value: str) -> List[str]:
    """Extract encoded and decoded URL userinfo without treating the host as secret."""
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
    except ValueError:
        return []
    if "@" not in parsed.netloc:
        return []

    userinfo = parsed.netloc.rsplit("@", 1)[0]
    values = [value, userinfo, unquote(userinfo)]
    # Redact a standalone credential when it is distinctive enough not to turn
    # every one-letter match in a diagnostic into noise. The complete URL and
    # userinfo are always redacted, regardless of length.
    credential = parsed.password if parsed.password is not None else parsed.username
    if credential and len(credential) >= 4:
        values.extend([credential, unquote(credential)])
    return [item for item in values if item]


def _pi_redaction_values(config: _PiRuntimeConfig) -> List[str]:
    values = [config.api_key]
    values.extend(_url_credential_values(config.base_url))
    for name in _PROXY_ENV_NAMES:
        proxy = os.environ.get(name)
        if proxy:
            values.extend(_url_credential_values(proxy))
    return values


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(part for part in parts if part).strip()


def _event_error(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        message = value.get("message") or value.get("errorMessage")
        if isinstance(message, str):
            return message.strip()
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


def _tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        text = _message_text(result)
        if text:
            return text
    return _event_error(result)


def _usage(message: Dict[str, Any]) -> Dict[str, int]:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return {}
    field_map = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cacheRead": "input_cached_tokens",
        "cacheWrite": "input_cache_write_tokens",
        "reasoning": "output_reasoning_tokens",
        "totalTokens": "total_tokens",
    }
    parsed: Dict[str, int] = {}
    for source, target in field_map.items():
        value = raw.get(source)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parsed[target] = int(value)
    return parsed


def _parse_events(stdout: str) -> _PiEventResult:
    """Parse Pi's JSONL contract, preserving protocol and agent failures."""
    parsed = _PiEventResult()
    assistant_messages: List[Dict[str, Any]] = []
    pending_retry_errors: List[str] = []

    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            parsed.fatal_errors.append(
                f"Pi emitted invalid JSONL on line {line_number}: {exc.msg}"
            )
            continue
        if not isinstance(event, dict):
            parsed.fatal_errors.append(
                f"Pi emitted non-object JSONL on line {line_number}"
            )
            continue

        event_type = str(event.get("type") or "")
        if event_type == "tool_execution_start":
            parsed.tool_calls += 1
        elif event_type == "tool_execution_end" and event.get("isError") is True:
            name = str(event.get("toolName") or "unknown")
            parsed.errors.append(
                f"{name}: {_tool_result_text(event.get('result')) or 'tool failed'}"
            )
        elif event_type == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            assistant_messages.append(message)
            parsed.rounds += 1
            for key, value in _usage(message).items():
                parsed.usage[key] = parsed.usage.get(key, 0) + value
            text = _message_text(message)
            if text:
                parsed.summary = text
            error_message = message.get("errorMessage")
            stop_reason = str(message.get("stopReason") or "").lower()
            if stop_reason == "error":
                pending_retry_errors.append(
                    f"Pi assistant error: "
                    f"{_event_error(error_message) if error_message else 'request failed'}"
                )
            elif error_message:
                parsed.fatal_errors.append(
                    f"Pi assistant error: {_event_error(error_message)}"
                )
            if stop_reason == "length":
                parsed.truncated = True
            elif stop_reason == "aborted":
                parsed.fatal_errors.append(
                    f"Pi assistant stopped with reason {stop_reason!r}"
                )
        elif event_type == "agent_end":
            parsed.agent_ended = True
            if not parsed.summary:
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if (
                            isinstance(message, dict)
                            and message.get("role") == "assistant"
                        ):
                            text = _message_text(message)
                            if text:
                                parsed.summary = text
                                if not assistant_messages:
                                    parsed.rounds = 1
                                    parsed.usage.update(_usage(message))
                                break
        elif event_type == "extension_error":
            extension_event = str(event.get("event") or "unknown event")
            detail = event.get("error", event.get("message", event))
            parsed.fatal_errors.append(
                f"Pi extension error during {extension_event}: {_event_error(detail)}"
            )
        elif event_type == "error":
            detail = event.get("message", event.get("error", event))
            parsed.fatal_errors.append(f"Pi error: {_event_error(detail)}")
        elif event_type == "auto_retry_end":
            if event.get("success") is True:
                parsed.errors.extend(
                    f"recovered after {error}" for error in pending_retry_errors
                )
                pending_retry_errors.clear()
            elif event.get("success") is False:
                detail = event.get("finalError") or "provider retry failed"
                parsed.fatal_errors.append(f"Pi retry failed: {_event_error(detail)}")
                pending_retry_errors.clear()

    if not parsed.agent_ended:
        parsed.fatal_errors.append("Pi JSONL stream ended without agent_end")
    parsed.fatal_errors.extend(pending_retry_errors)
    if not parsed.summary and not parsed.truncated:
        parsed.fatal_errors.append("Pi completed without a final assistant message")
    return parsed


def _positive_run_limit(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise PiExecutorError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PiExecutorError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise PiExecutorError(f"{name} must be a positive integer")
    return parsed


async def _settle_cleanup_task(
    task: "asyncio.Task[Any]",
) -> tuple[Any, Optional[BaseException], bool]:
    """Settle a cleanup task despite caller cancellation and report that cancellation."""
    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                caller_cancelled = True
                current.uncancel()
                continue
            # The cleanup task itself was cancelled.
            break
        except BaseException:
            break

    if task.cancelled():
        return None, asyncio.CancelledError(), caller_cancelled
    try:
        return task.result(), None, caller_cancelled
    except BaseException as exc:  # noqa: BLE001 - caller decides cleanup precedence
        return None, exc, caller_cancelled


async def _kill_and_wait(
    process: Any, communication_task: "asyncio.Task[tuple[bytes, bytes]]"
) -> tuple[bytes, bytes, Optional[str]]:
    """Kill Pi, drain its pipes, and explicitly reap it without masking the caller."""
    try:
        if process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass

    communication_result, error, cancelled_while_communicating = (
        await _settle_cleanup_task(communication_task)
    )
    if error is not None:
        stdout_bytes, stderr_bytes = b"", b""
        if isinstance(error, asyncio.CancelledError):
            communication_error = "Pi communication was cancelled during cleanup"
        else:
            communication_error = (
                f"Pi communication failed: {type(error).__name__}: {error}"
            )
    else:
        stdout_bytes, stderr_bytes = communication_result
        communication_error = None

    wait_task = asyncio.create_task(process.wait())
    _wait_result, wait_error, cancelled_while_waiting = await _settle_cleanup_task(
        wait_task
    )
    if wait_error is not None and not isinstance(wait_error, ProcessLookupError):
        detail = f"Pi process wait failed: {type(wait_error).__name__}: {wait_error}"
        communication_error = (
            f"{communication_error}; {detail}" if communication_error else detail
        )
    if cancelled_while_communicating or cancelled_while_waiting:
        raise asyncio.CancelledError
    return stdout_bytes, stderr_bytes, communication_error


async def _close_gateway(gateway: _VaultToolGateway) -> None:
    """Drain the gateway before propagating cancellation to the caller."""
    close_task = asyncio.create_task(gateway.aclose())
    _result, error, caller_cancelled = await _settle_cleanup_task(close_task)
    if caller_cancelled:
        raise asyncio.CancelledError
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            raise PiExecutorError("Pi vault gateway close was unexpectedly cancelled")
        raise error


async def _invoke_pi(
    vault_root: Path,
    *,
    prompt: str,
    system_prompt: str,
    schemas: Sequence[Dict[str, Any]],
    config: _PiRuntimeConfig,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
    required_notes: Sequence[str] = (),
    forbidden_folders: Sequence[str] = (),
    user_id: str = "",
) -> tuple[_PiEventResult, _VaultToolGateway]:
    """Run Pi and preserve gateway audit state for every post-start failure."""
    started_ns = time.time_ns()
    max_tool_rounds = _positive_run_limit(max_tool_rounds, name="Pi max_tool_rounds")
    max_tool_calls = _positive_run_limit(max_tool_calls, name="Pi max_tool_calls")
    loop = asyncio.get_running_loop()
    limit_signal = asyncio.Event()
    gateway = _VaultToolGateway(
        vault_root,
        schemas,
        max_tool_calls=max_tool_calls,
        on_limit=lambda: loop.call_soon_threadsafe(limit_signal.set),
        required_notes=required_notes,
        forbidden_folders=forbidden_folders,
        user_id=user_id,
    )
    events: Optional[_PiEventResult] = None
    redaction_values = [
        *_pi_redaction_values(config),
        _pi_literal_config_value(config.api_key),
        gateway.token,
    ]

    with tempfile.TemporaryDirectory(prefix="chronicle-pi-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        gateway.__enter__()
        try:
            extension_path = temp_dir / "chronicle-runtime.mjs"
            extension_path.write_text(
                _extension_source(
                    schemas,
                    gateway_url=gateway.url,
                    token=gateway.token,
                    temperature=config.temperature,
                    max_tool_rounds=max_tool_rounds,
                    max_tool_calls=max_tool_calls,
                ),
                encoding="utf-8",
            )
            extension_path.chmod(0o600)
            models_path = temp_dir / "models.json"
            models_path.write_text(
                json.dumps(_models_payload(config), indent=2),
                encoding="utf-8",
            )
            models_path.chmod(0o600)
            system_prompt_path = temp_dir / "system-prompt.md"
            effective_system_prompt = system_prompt
            if config.system_prompt_prefix:
                effective_system_prompt = (
                    f"{config.system_prompt_prefix}\n\n{system_prompt}"
                )
            system_prompt_path.write_text(effective_system_prompt, encoding="utf-8")
            system_prompt_path.chmod(0o600)

            command = [
                config.binary,
                "--mode",
                "json",
                "--provider",
                config.provider,
                "--model",
                config.model,
                "--thinking",
                config.thinking,
                "--system-prompt",
                str(system_prompt_path),
                "--no-session",
                "--offline",
                "--no-context-files",
                "--no-extensions",
                "--no-prompt-templates",
                "--no-themes",
                "--no-builtin-tools",
            ]
            if schemas:
                # The vault's shape travels as a skill rather than more system prompt,
                # so it is generated from the same templates the vault is scaffolded
                # from and cannot drift from what verify_vault enforces.
                skill_path = write_skill(temp_dir)
                skill_path.chmod(0o600)
                command.extend(["--skill", str(skill_path)])
            else:
                # Final synthesis: no tools, and nothing to teach.
                command.append("--no-skills")
            command.extend(["-e", str(extension_path)])
            if schemas:
                tool_names = ",".join(
                    str(schema["function"]["name"]) for schema in schemas
                )
                command.extend(["--tools", tool_names])

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(vault_root),
                    env=_pi_subprocess_env(temp_dir),
                )
            except OSError as exc:
                raise PiExecutorError(f"Pi failed to start: {exc}") from exc

            communication_task = asyncio.create_task(
                process.communicate(prompt.encode("utf-8"))
            )
            limit_task = asyncio.create_task(limit_signal.wait())
            failures: List[str] = []
            terminated_by_chronicle = False
            try:
                done, _pending = await asyncio.wait(
                    {communication_task, limit_task},
                    timeout=config.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    terminated_by_chronicle = True
                    failures.append(f"Pi timed out after {config.timeout_seconds}s")
                    stdout_bytes, stderr_bytes, cleanup_error = await _kill_and_wait(
                        process, communication_task
                    )
                    if cleanup_error:
                        failures.append(cleanup_error)
                elif limit_task in done and gateway.limit_error:
                    terminated_by_chronicle = True
                    failures.append(gateway.limit_error)
                    stdout_bytes, stderr_bytes, cleanup_error = await _kill_and_wait(
                        process, communication_task
                    )
                    if cleanup_error:
                        failures.append(cleanup_error)
                else:
                    try:
                        stdout_bytes, stderr_bytes = await communication_task
                    except Exception as exc:  # noqa: BLE001 - return auditable failure
                        terminated_by_chronicle = True
                        failures.append(
                            f"Pi communication failed: {type(exc).__name__}: {exc}"
                        )
                        stdout_bytes, stderr_bytes, cleanup_error = (
                            await _kill_and_wait(process, communication_task)
                        )
                        if cleanup_error:
                            failures.append(cleanup_error)
            except asyncio.CancelledError:
                await _kill_and_wait(process, communication_task)
                raise
            finally:
                limit_task.cancel()
                await asyncio.gather(limit_task, return_exceptions=True)

            try:
                stdout = stdout_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                failures.append(f"Pi emitted non-UTF-8 stdout: {exc}")
            try:
                stderr = stderr_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                failures.append(f"Pi emitted non-UTF-8 stderr: {exc}")

            redacted_stderr = _redact(
                stderr[-_STDERR_TAIL_CHARS:].strip(), *redaction_values
            )
            if not terminated_by_chronicle and process.returncode != 0:
                suffix = f": {redacted_stderr}" if redacted_stderr else ""
                failures.append(f"Pi exited with status {process.returncode}{suffix}")
            elif redacted_stderr:
                logger.warning("Pi stderr: %s", redacted_stderr)

            events = _parse_events(stdout)
            if gateway.limit_error and gateway.limit_error not in failures:
                failures.append(gateway.limit_error)
            events.fatal_errors.extend(failures)
        finally:
            await _close_gateway(gateway)

    if events is None:  # pragma: no cover - every non-exception path assigns events
        raise PiExecutorError("Pi finished without a result")
    # An authenticated /limit request may already be admitted when shutdown starts.
    # Gateway close drains it before freezing state, so reconcile the final value only
    # now; copying it before close would silently lose that hard-limit outcome.
    if gateway.limit_error and gateway.limit_error not in events.fatal_errors:
        events.fatal_errors.append(gateway.limit_error)
    events.summary = _redact(events.summary, *redaction_values)
    events.errors = [
        _redact(error, *redaction_values) for error in [*events.errors, *gateway.errors]
    ]
    events.fatal_errors = [
        _redact(error, *redaction_values) for error in events.fatal_errors
    ]
    events.errors.extend(events.fatal_errors)
    if events.fatal_errors or gateway.close_error:
        events.truncated = True
    elif events.truncated:
        events.errors.append("Pi response was truncated")
    record_llm_usage_span(
        "pi_model_run",
        provider=config.provider,
        model=config.model,
        usage=events.usage,
        start_time_ns=started_ns,
        end_time_ns=time.time_ns(),
        attributes={
            "chronicle.memory.executor": "pi",
            "chronicle.memory.attempt": current_memory_attempt(),
            "chronicle.memory.pi.usage_scope": "subprocess_aggregate",
            "chronicle.memory.pi.phase": "tool_agent" if schemas else "final_synthesis",
            "chronicle.memory.rounds": events.rounds,
            "chronicle.memory.tool_calls": max(events.tool_calls, gateway.call_count),
            "gen_ai.request.temperature": config.temperature,
            "gen_ai.request.max_tokens": config.max_tokens,
            "chronicle.memory.pi.context_window": config.context_window,
            "chronicle.memory.pi.thinking": config.thinking,
            "chronicle.memory.truncated": events.truncated,
            "chronicle.memory.error_count": len(events.errors),
        },
    )
    return events, gateway


class PiMemoryAgent:
    """Run Chronicle's write agent through the isolated Pi CLI harness."""

    def __init__(
        self,
        vault_root: Path,
        operation: str = "memory_write",
        *,
        force_fallback: bool = False,
    ):
        self.root = Path(vault_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.operation = operation
        self.force_fallback = force_fallback

    async def run(
        self,
        transcript: str,
        conversation_id: str,
        *,
        date: Optional[str] = None,
        duration_minutes: Optional[float] = None,
        title: Optional[str] = None,
        vault_summary: str = "",
        guidance: str = "",
        record: str = "conversation",
    ) -> MemoryAgentResult:
        config = _resolve_pi_config(self.operation, force_fallback=self.force_fallback)
        date = date or datetime.now(timezone.utc).isoformat()
        system_prompt = await _get_prompt(
            AGENT_SYSTEM_PROMPT_ID, DEFAULT_AGENT_SYSTEM_PROMPT, vault_summary
        )
        task = build_write_task(
            transcript,
            conversation_id,
            date=date,
            duration_minutes=duration_minutes,
            title=title,
            guidance=guidance,
            record=record,
        )
        with memory_span(
            "pi_memory_agent",
            attributes={
                "openinference.span.kind": "AGENT",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": config.provider,
                "gen_ai.request.model": config.model,
                "gen_ai.conversation.id": conversation_id,
                "session.id": conversation_id,
                "langfuse.session.id": conversation_id,
                "chronicle.memory.operation": self.operation,
                "chronicle.memory.executor": "pi",
                "chronicle.memory.attempt": current_memory_attempt(),
                "chronicle.memory.force_fallback": self.force_fallback,
                "chronicle.memory.transcript_chars": len(transcript),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": conversation_id,
                    "transcript": text_payload(transcript),
                    "title": text_payload(title),
                    "guidance": text_payload(guidance),
                },
            )
            events, gateway = await _invoke_pi(
                self.root,
                prompt=task,
                system_prompt=system_prompt,
                schemas=VAULT_TOOL_SCHEMAS,
                config=config,
                max_tool_rounds=MAX_TOOL_ROUNDS,
                max_tool_calls=MAX_PI_WRITE_TOOL_CALLS,
                required_notes=required_notes(record, conversation_id),
                forbidden_folders=forbidden_folders(record),
            )
            result = MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=max(events.rounds, 1),
                touched=sorted(gateway.tools.touched),
                summary=events.summary or "(Pi failed before completing)",
                tool_calls=max(events.tool_calls, gateway.call_count),
                removed=list(gateway.tools.removed),
                errors=events.errors,
                usage=events.usage,
                truncated=events.truncated,
                verified=gateway.tools.verified,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": not result.truncated,
                    "chronicle.memory.rounds": result.rounds,
                    "chronicle.memory.tool_calls": result.tool_calls,
                    "chronicle.memory.touched_count": len(result.touched),
                    "chronicle.memory.removed_count": len(result.removed),
                    "chronicle.memory.error_count": len(result.errors),
                    "chronicle.memory.truncated": result.truncated,
                    **{
                        f"chronicle.memory.usage.{key}": value
                        for key, value in result.usage.items()
                    },
                },
            )
            set_observation_io(
                span,
                output={
                    "summary": text_payload(result.summary),
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "touched_count": len(result.touched),
                    "removed_count": len(result.removed),
                    "error_count": len(result.errors),
                    "truncated": result.truncated,
                },
            )
            return result


def _pi_final_search_prompt(query: str, read_notes: Dict[str, str]) -> str:
    """Build a no-tools synthesis request from evidence Pi already chose to read."""
    return _search_final_synthesis_prompt(query, read_notes)


async def _search_vault_with_pi_impl(
    query: str,
    vault_root: Path,
    *,
    operation: str = "memory_search",
    max_rounds: int = MAX_SEARCH_ROUNDS,
    vault_summary: str = "",
    user_id: str = "",
) -> VaultSearchResult:
    """Run Chronicle's read-only retrieval agent through Pi."""
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    max_rounds = _positive_run_limit(max_rounds, name="Pi search max_rounds")
    config = _resolve_pi_config(operation)
    system_prompt = await _get_prompt(
        "memory.search_system", SEARCH_SYSTEM_PROMPT, vault_summary
    )
    prompt = (
        f"{query}\n\n"
        f"Use at most {max_rounds} tool rounds. Return the answer when you have enough evidence."
    )
    events, gateway = await _invoke_pi(
        root,
        prompt=prompt,
        system_prompt=system_prompt,
        schemas=VAULT_SEARCH_TOOL_SCHEMAS,
        config=config,
        max_tool_rounds=max_rounds,
        max_tool_calls=max_rounds * _PI_TOOL_CALLS_PER_SEARCH_ROUND,
        user_id=user_id,
    )
    answer = PI_SEARCH_FAILURE_ANSWER if events.truncated else events.summary
    rounds = events.rounds
    usage = dict(events.usage)
    errors = list(events.errors)
    warnings: List[str] = []
    final_synthesis_used = False

    # Pi's extension must abort an attempted tool turn beyond the hard cap so a
    # poorly behaved model cannot keep reading indefinitely. If it had already read
    # evidence, retain that safety property while giving Pi one fresh, no-tools
    # completion to synthesize an answer or abstain. This remains the Pi backend: the
    # same isolated Pi CLI/model configuration performs both phases, and the second
    # process is given neither Chronicle's extension nor any built-in Pi tools.
    if gateway.limit_error and gateway.read_notes:
        final_synthesis_used = True
        try:
            final_events, _final_gateway = await _invoke_pi(
                root,
                prompt=_pi_final_search_prompt(query, gateway.read_notes),
                system_prompt=(
                    f"{system_prompt}\n\n{SEARCH_FINAL_SYNTHESIS_SYSTEM_SUFFIX}"
                ),
                schemas=(),
                config=config,
                max_tool_rounds=1,
                max_tool_calls=1,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the audited first phase
            detail = _redact(
                f"{type(exc).__name__}: {exc}",
                *_pi_redaction_values(config),
            )
            errors.append(f"Pi final search synthesis failed: {detail}")
            logger.error("Pi final search synthesis failed: %s", detail)
        else:
            rounds += final_events.rounds
            for key, value in final_events.usage.items():
                usage[key] = usage.get(key, 0) + value
            errors.extend(final_events.errors)
            if not final_events.truncated and final_events.summary:
                answer = final_events.summary
                # A hard cap deliberately kills Pi before `agent_end`; those two
                # first-phase events describe why final synthesis was needed, not a
                # failed answer. Preserve them as warnings so callers and benchmark
                # manifests retain the audit without treating successful recovery as
                # an error.
                recovered_events = {
                    gateway.limit_error,
                    "Pi JSONL stream ended without agent_end",
                }
                retained_errors = []
                for error in errors:
                    if error in recovered_events:
                        warnings.append(error)
                    else:
                        retained_errors.append(error)
                errors = retained_errors

    return VaultSearchResult(
        answer=answer,
        notes=[
            {"path": path, "content": content}
            for path, content in gateway.read_notes.items()
        ],
        rounds=max(rounds, 1),
        usage=usage,
        errors=errors,
        warnings=warnings,
        tool_calls=gateway.call_count,
        final_synthesis_used=final_synthesis_used,
        truncated=answer.strip() == PI_SEARCH_FAILURE_ANSWER,
    )


async def search_vault_with_pi(
    query: str,
    vault_root: Path,
    *,
    operation: str = "memory_search",
    max_rounds: int = MAX_SEARCH_ROUNDS,
    vault_summary: str = "",
    user_id: str = "",
) -> VaultSearchResult:
    """Trace one complete Pi retrieval, including optional cap synthesis."""

    with memory_span(
        "pi_memory_search_agent",
        attributes={
            "openinference.span.kind": "AGENT",
            "gen_ai.operation.name": "invoke_agent",
            "chronicle.memory.operation": operation,
            "chronicle.memory.executor": "pi",
            "chronicle.memory.attempt": current_memory_attempt(),
            "chronicle.memory.query_chars": len(query),
            "chronicle.memory.max_rounds": max_rounds,
        },
    ) as span:
        set_observation_io(
            span,
            input={
                "query": text_payload(query),
                "vault_summary": text_payload(vault_summary),
                "max_rounds": max_rounds,
            },
        )
        result = await _search_vault_with_pi_impl(
            query,
            vault_root,
            operation=operation,
            max_rounds=max_rounds,
            vault_summary=vault_summary,
        )
        set_safe_span_attributes(
            span,
            {
                "chronicle.memory.success": not result.truncated,
                "chronicle.memory.rounds": result.rounds,
                "chronicle.memory.tool_calls": result.tool_calls,
                "chronicle.memory.notes_read_count": len(result.notes),
                "chronicle.memory.error_count": len(result.errors),
                "chronicle.memory.warning_count": len(result.warnings),
                "chronicle.memory.final_synthesis_used": result.final_synthesis_used,
                "chronicle.memory.truncated": result.truncated,
                **{
                    f"chronicle.memory.usage.{key}": value
                    for key, value in result.usage.items()
                },
            },
        )
        set_observation_io(
            span,
            output={
                "answer": text_payload(result.answer),
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "notes_read_count": len(result.notes),
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
                "final_synthesis_used": result.final_synthesis_used,
                "truncated": result.truncated,
            },
        )
        return result
