"""Structured image understanding through a selected Chronicle model route.

Callers name an operation and provide images plus a JSON schema. The model registry
owns the provider/model/endpoint; this module owns image encoding, capability checks,
bounded retries, JSON parsing, and schema validation. Codex remains an explicit adapter
for deployments that select it, but it is no longer silently baked into every caller.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import ValidationError, validate

from advanced_omi_backend.llm_client import async_chat_with_tools
from advanced_omi_backend.model_registry import get_models_registry

from .codex_vision import (
    CodexVisionError,
    CodexVisionUnavailable,
    codex_vision_settings,
    run_codex_vision,
)

DEFAULT_TIMEOUT_SECONDS = 600


class VisionError(RuntimeError):
    """A selected structured-vision route ran but produced no usable result."""


class VisionUnavailable(VisionError):
    """The selected adapter or model cannot currently perform vision."""


def vision_route_identity(settings: Mapping[str, Any]) -> str:
    """Return a stable cache identity for the effective vision route.

    Persisted visual descriptions must not survive a model-route change invisibly.
    Callers can combine this identity with their own prompt/schema revision.
    """

    backend = str(settings.get("backend") or "model").strip().lower()
    if backend == "codex":
        codex = settings.get("codex") or {}
        return f"codex:{str(codex.get('model') or 'unknown').strip()}"
    if backend != "model":
        raise VisionUnavailable(f"unsupported vision backend: {backend}")

    operation = str(settings.get("operation") or "").strip()
    registry = get_models_registry()
    if registry is None:
        raise VisionUnavailable("Model registry not loaded")
    try:
        resolved = registry.get_llm_operation(operation)
    except (RuntimeError, ValueError) as exc:
        raise VisionUnavailable(str(exc)) from exc
    model = resolved.model_def
    return ":".join(
        (
            "model",
            operation,
            str(model.model_provider).strip(),
            str(model.name).strip(),
            str(model.model_name).strip(),
        )
    )


def structured_vision_settings(
    settings: Mapping[str, Any],
    *,
    label: str,
    default_operation: str,
    codex_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the small route interface shared by all vision callers."""

    backend = str(settings.get("backend") or "model").strip().lower()
    if backend not in {"model", "codex"}:
        raise ValueError(f"{label}.backend must be model or codex")
    timeout = int(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if timeout <= 0:
        raise ValueError(f"{label}.timeout_seconds must be positive")
    if backend == "model":
        operation = str(settings.get("operation") or default_operation).strip()
        if not operation:
            raise ValueError(f"{label}.operation must be configured")
        return {
            "backend": backend,
            "operation": operation,
            "timeout_seconds": timeout,
        }
    return {
        "backend": backend,
        "timeout_seconds": timeout,
        "codex": codex_vision_settings(
            codex_settings or {}, label="vision.backends.codex"
        ),
    }


def _json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response did not contain a JSON object")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("response JSON was not an object")
    return value


def _messages(
    prompt: str,
    images: Sequence[tuple[str, bytes]],
    schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"{prompt}\n\nReturn JSON only and match this required JSON schema:\n"
                f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
            ),
        }
    ]
    for filename, data in images:
        content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{encoded}"},
            }
        )
    return [{"role": "user", "content": content}]


async def run_structured_vision(
    prompt: str,
    images: Sequence[tuple[str, bytes]],
    schema: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one selected vision route and return schema-validated JSON."""

    if not images:
        raise VisionError("a vision run needs at least one image")
    backend = str(settings.get("backend") or "model").strip().lower()
    if backend == "codex":
        try:
            return await run_codex_vision(
                prompt, images, schema, settings.get("codex") or {}
            )
        except CodexVisionUnavailable as exc:
            raise VisionUnavailable(str(exc)) from exc
        except CodexVisionError as exc:
            raise VisionError(str(exc)) from exc
    if backend != "model":
        raise VisionUnavailable(f"unsupported vision backend: {backend}")

    operation = str(settings.get("operation") or "").strip()
    registry = get_models_registry()
    if registry is None:
        raise VisionUnavailable("Model registry not loaded")
    try:
        resolved = registry.get_llm_operation(operation)
    except (RuntimeError, ValueError) as exc:
        raise VisionUnavailable(str(exc)) from exc
    if "vision" not in {item.lower() for item in resolved.model_def.capabilities}:
        raise VisionUnavailable(
            f"model {resolved.model_def.name} is not configured for vision"
        )

    timeout = int(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if timeout <= 0:
        raise ValueError("vision timeout_seconds must be positive")
    messages = _messages(prompt, images, schema)
    last_error: Exception | None = None
    for attempt in range(2):
        request_messages = list(messages)
        if attempt:
            request_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON or did not match the "
                        "schema. Return one corrected JSON object only."
                    ),
                }
            )
        try:
            response = await async_chat_with_tools(
                request_messages,
                operation=operation,
                timeout_seconds=timeout,
            )
            content = response.choices[0].message.content or ""
            value = _json_object(content)
            validate(instance=value, schema=dict(schema))
            return value
        except asyncio.CancelledError:
            raise
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        except Exception as exc:  # provider/network failures are service-level faults
            raise VisionError(f"vision operation {operation!r} failed: {exc}") from exc
    raise VisionError(
        f"vision operation {operation!r} produced no schema-valid JSON: {last_error}"
    )
