"""Inspectable effective routing for Chronicle's selected model workloads.

The model registry already owns named LLM operations. A few higher-level modules also
choose an adapter (direct, Pi, Codex, or the registry-backed vision adapter), and those
choices used to be visible only by reading several unrelated YAML sections. This module
turns the selected runtime routes into one secret-free report for the Settings page,
startup logs, and a CLI.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

from advanced_omi_backend.model_registry import AppModels, get_models_registry


def _get(mapping: Any, *path: str, default: Any = None) -> Any:
    value = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return default
        value = value.get(key)
        if value is None:
            return default
    return value


def endpoint_location(provider: str, endpoint: str) -> str:
    """Classify a route without exposing credentials or pretending Tailnet is cloud."""

    normalized_provider = provider.strip().lower()
    if normalized_provider == "codex":
        return "external"
    if normalized_provider in {"llamacpp", "ollama", "local"}:
        return "self-hosted"
    hostname = (urlparse(endpoint).hostname or "").lower()
    if not hostname:
        return "unknown"
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith(
        (".local", ".internal", ".ts.net")
    ):
        return "self-hosted"
    if "." not in hostname:
        return "self-hosted"
    try:
        if ipaddress.ip_address(hostname).is_private:
            return "self-hosted"
    except ValueError:
        pass
    return "external"


def public_endpoint(endpoint: str) -> str:
    """Return a display-safe endpoint with credentials and query data removed."""

    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.hostname:
        return endpoint
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    return urlunparse((parsed.scheme, f"{hostname}{port}", parsed.path, "", "", ""))


def _operation_route(
    registry: AppModels, *, workload: str, adapter: str, operation: str
) -> dict[str, Any]:
    explanation = registry.explain_llm_operation(operation)
    resolved = registry.get_llm_operation(operation)
    endpoint = public_endpoint(resolved.base_url)
    provider = str(explanation["provider"] or "")
    return {
        "workload": workload,
        "adapter": adapter,
        "operation": operation,
        "model": str(explanation["model"] or ""),
        "model_name": str(explanation["model_name"] or ""),
        "provider": provider,
        "endpoint": endpoint,
        "location": endpoint_location(provider, endpoint),
        "source": str(explanation["source"] or ""),
        "max_tokens": resolved.max_tokens,
        "reasoning_effort": resolved.reasoning_effort,
        "response_format": (
            "json" if resolved.response_format == {"type": "json_object"} else "text"
        ),
    }


def _codex_route(
    *, workload: str, model: str, source: str, adapter: str = "codex"
) -> dict[str, Any]:
    return {
        "workload": workload,
        "adapter": adapter,
        "operation": "",
        "model": model or "codex-default",
        "model_name": model or "codex-default",
        "provider": "codex",
        "endpoint": "Codex CLI / ChatGPT",
        "location": "external",
        "source": source,
        "max_tokens": None,
        "reasoning_effort": None,
        "response_format": "adapter-defined",
    }


def effective_model_routes(
    config: Mapping[str, Any], registry: AppModels | None = None
) -> list[dict[str, Any]]:
    """Return every selected high-level LLM/vision route in a stable order."""

    registry = registry or get_models_registry()
    if registry is None:
        raise RuntimeError("Model registry not loaded")
    routes: list[dict[str, Any]] = []

    for workload, kind, operation in (
        (
            "memory.write",
            str(_get(config, "memory", "agents", "write", "backend", default="direct")),
            "memory_write",
        ),
        (
            "memory.search",
            str(
                _get(config, "memory", "agents", "search", "backend", default="direct")
            ),
            "memory_search",
        ),
    ):
        if kind == "codex":
            routes.append(
                _codex_route(
                    workload=workload,
                    model=str(
                        _get(config, "memory", "backends", "codex", "model", default="")
                    ),
                    source="memory.backends.codex.model",
                )
            )
        else:
            routes.append(
                _operation_route(
                    registry, workload=workload, adapter=kind, operation=operation
                )
            )

    timeline_executor = str(_get(config, "timeline", "executor", default="codex"))
    if timeline_executor == "codex":
        routes.append(
            _codex_route(
                workload="timeline.segmentation",
                model=str(_get(config, "timeline", "codex", "model", default="")),
                source="timeline.codex.model",
            )
        )
    else:
        timeline_operation = str(
            _get(
                config,
                "timeline",
                "pi",
                "operation",
                default="timeline_segmentation",
            )
        )
        routes.append(
            _operation_route(
                registry,
                workload="timeline.segmentation",
                adapter=timeline_executor,
                operation=timeline_operation,
            )
        )

    vision_specs = (
        (
            "manual_memories.image",
            ("manual_memories", "agents", "analyze_image"),
            "manual_memory_image",
        ),
        (
            "timeline.thumbnail",
            ("timeline", "thumbnails"),
            "timeline_thumbnail",
        ),
        (
            "timeline.immich_visual_evidence",
            ("timeline", "immich_visual_evidence"),
            "immich_visual_evidence",
        ),
    )
    for workload, path, default_operation in vision_specs:
        settings = _get(config, *path, default={}) or {}
        backend = str(_get(settings, "backend", default="model"))
        if backend == "codex":
            codex_settings = (
                _get(config, "vision", "backends", "codex", default={}) or {}
            )
            routes.append(
                _codex_route(
                    workload=workload,
                    model=str(_get(codex_settings, "model", default="")),
                    source="vision.backends.codex.model",
                )
            )
        else:
            operation = str(_get(settings, "operation", default=default_operation))
            routes.append(
                _operation_route(
                    registry,
                    workload=workload,
                    adapter=backend,
                    operation=operation,
                )
            )
    return routes


def effective_operation_routes(
    registry: AppModels | None = None,
) -> list[dict[str, Any]]:
    """Resolve every named LLM operation for a complete cloud/local audit."""

    registry = registry or get_models_registry()
    if registry is None:
        raise RuntimeError("Model registry not loaded")
    return [
        _operation_route(
            registry,
            workload=f"operation.{operation}",
            adapter="model",
            operation=operation,
        )
        for operation in sorted(registry.llm_operations)
    ]


def format_model_routes(routes: list[dict[str, Any]]) -> str:
    """Render a copy/paste-friendly table without third-party terminal styling."""

    columns = (
        ("workload", "WORKLOAD"),
        ("adapter", "ADAPTER"),
        ("operation", "OPERATION"),
        ("model", "MODEL"),
        ("provider", "PROVIDER"),
        ("location", "LOCATION"),
        ("endpoint", "ENDPOINT"),
        ("max_tokens", "MAX TOKENS"),
        ("reasoning_effort", "REASONING"),
        ("source", "SELECTED BY"),
    )
    widths = {
        key: max(
            len(label),
            *(
                len(str(row.get(key, "") if row.get(key) is not None else ""))
                for row in routes
            ),
        )
        for key, label in columns
    }
    header = "  ".join(label.ljust(widths[key]) for key, label in columns)
    divider = "  ".join("-" * widths[key] for key, _label in columns)
    lines = [header, divider]
    lines.extend(
        "  ".join(
            str(row.get(key, "") if row.get(key) is not None else "").ljust(widths[key])
            for key, _label in columns
        )
        for row in routes
    )
    return "\n".join(lines)
