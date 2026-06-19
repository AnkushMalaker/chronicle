#!/usr/bin/env python3
"""
Chronicle Service Management
Start, stop, and manage configured services
"""

import argparse
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from setup_utils import ensure_tailscale_cert, read_env_value

console = Console()


def load_config_yml():
    """Load config.yml from repository root"""
    config_path = Path(__file__).parent / "config" / "config.yml"
    if not config_path.exists():
        return None

    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        console.print(
            f"[yellow]⚠️  Warning: Could not load config/config.yml: {e}[/yellow]"
        )
        return None


def _engine_config():
    """Resolve (engine, compose_argv). Precedence: env > config.yml > docker default.

    The compose front-end differs per engine: docker uses the compose plugin
    (`docker compose`); podman uses `podman-compose`, whose CLI path performs CDI
    GPU injection (podman 4.x's docker-compat socket API does not).
    """
    cfg = load_config_yml() or {}
    engine = (
        os.environ.get("CONTAINER_ENGINE") or cfg.get("container_engine") or "docker"
    )
    default_compose = "podman-compose" if engine == "podman" else "docker compose"
    compose_raw = (
        os.environ.get("COMPOSE_CMD") or cfg.get("compose_cmd") or default_compose
    )
    return engine, shlex.split(compose_raw)


def container_engine():
    """Container engine binary: 'docker' (default) or 'podman'."""
    return _engine_config()[0]


def compose_base():
    """Base argv for compose up/down/build/restart.

    e.g. ['docker', 'compose'] or ['podman-compose']. shlex-split handles both the
    two-token plugin form and the single-token podman-compose form transparently.
    """
    return list(_engine_config()[1])


def compose_ps_json(service_path):
    """List a service's containers as normalized {name, state, status, health} dicts.

    Abstracts over docker vs podman, whose `ps` outputs differ:
    - docker: `docker compose ps --format json` (one JSON object per line; native
      fields Name/State/Status/Health).
    - podman: podman-compose's `ps` is NOT docker-compatible, so query the engine
      directly, scoped by the compose project working-dir label that podman-compose
      stamps on every container. Native podman fields are Names[]/State/Status, with
      health embedded in Status (e.g. "Up 3 seconds (healthy)").

    Raises on a failed command; callers handle the exception.
    """
    engine = container_engine()
    service_path = Path(service_path)

    if engine == "podman":
        label = (
            "label=com.docker.compose.project.working_dir=" f"{service_path.resolve()}"
        )
        result = subprocess.run(
            [engine, "ps", "-a", "--filter", label, "--format", "json"],
            cwd=service_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "podman ps failed")
        out = result.stdout.strip()
        if not out:
            return []
        raw = (
            json.loads(out)
            if out.startswith("[")
            else [json.loads(line) for line in out.splitlines() if line.strip()]
        )
        containers = []
        for c in raw:
            names = c.get("Names") or []
            name = (
                names[0]
                if isinstance(names, list) and names
                else c.get("Name", "unknown")
            )
            status = c.get("Status", "unknown")
            if "(healthy)" in status:
                health = "healthy"
            elif "(unhealthy)" in status:
                health = "unhealthy"
            elif "starting" in status:
                health = "starting"
            else:
                health = "none"
            containers.append(
                {
                    "name": name,
                    "state": c.get("State", "unknown"),
                    "status": status,
                    "health": health,
                }
            )
        return containers

    # docker
    result = subprocess.run(
        compose_base() + ["ps", "--format", "json"],
        cwd=service_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose ps failed")
    containers = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "name": c.get("Name", "unknown"),
                "state": c.get("State", "unknown"),
                "status": c.get("Status", "unknown"),
                "health": c.get("Health", "none"),
            }
        )
    return containers


SERVICES = {
    "langfuse": {
        "path": "extras/langfuse",
        "compose_file": "docker-compose.yml",
        "description": "LangFuse Observability & Prompt Management",
        "ports": ["3002"],
        "health_endpoints": [
            ("langfuse", None, "3002", "/api/public/health"),
        ],
    },
    "backend": {
        "path": "backends/advanced",
        "compose_file": "docker-compose.yml",
        "description": "Advanced Backend + WebUI",
        "ports": ["8000", "5173"],
        "health_endpoints": [
            ("backend", "BACKEND_PUBLIC_PORT", "8000", "/readiness"),
        ],
    },
    "speaker-recognition": {
        "path": "extras/speaker-recognition",
        "compose_file": "docker-compose.yml",
        "description": "Speaker Recognition Service",
        "ports": ["8085", "5174/8444"],
        "health_endpoints": [
            ("speaker", "SPEAKER_SERVICE_PORT", "8085", "/health"),
        ],
    },
    "asr-services": {
        "path": "extras/asr-services",
        "compose_file": "docker-compose.yml",
        "description": "Parakeet ASR Service",
        "ports": ["8767"],
        "health_endpoints": [
            ("asr", "ASR_PORT", "8767", "/health"),
        ],
    },
    "openmemory-mcp": {
        "path": "extras/openmemory-mcp",
        "compose_file": "docker-compose.yml",
        "description": "OpenMemory MCP Server",
        "ports": ["8765"],
        "health_endpoints": [
            ("openmemory", None, "8765", "/docs"),
        ],
    },
    "llm-services": {
        "path": "extras/llm-services",
        "compose_file": "docker-compose.yml",
        "description": "Local LLM via llama.cpp (chat + embeddings)",
        "ports": ["8083", "8082"],
        "health_endpoints": [
            ("chat", "LLM_PORT", "8083", "/health"),
            ("embeddings", "EMBED_PORT", "8082", "/health"),
        ],
    },
    "wakeword-service": {
        "path": "extras/wakeword-service",
        "compose_file": "docker-compose.yml",
        "description": "Hermes Acoustic Wake-Word Detection",
        "ports": ["8771"],
        "health_endpoints": [
            ("wakeword", "WAKEWORD_PORT", "8771", "/health"),
        ],
    },
    "tts": {
        "path": "extras/tts",
        "compose_file": "docker-compose.yml",
        "description": "Text-to-Speech (TADA / Fish Speech / KittenTTS / Kokoro)",
        "ports": ["8770"],
        "health_endpoints": [
            ("tts", "TTS_PORT", "8770", "/health"),
        ],
    },
}


_DISCOVERY_NAMES = {
    "backend": "chronicle-backend",
    "speaker-recognition": "chronicle-speaker",
    "asr-services": "chronicle-asr",
    "openmemory-mcp": "chronicle-openmemory",
    "llm-services": "chronicle-llm",
    "wakeword-service": "chronicle-wakeword-service",
    "tts": "chronicle-tts",
}


_ADVERTISED_SERVICES_PATH = (
    Path(__file__).parent / "config" / "advertised-services.json"
)

_ASR_PROVIDER_LABELS = {
    "vibevoice": "VibeVoice ASR",
    "vibevoice-strixhalo": "VibeVoice ASR (Strix Halo)",
    "faster-whisper": "Faster Whisper ASR",
    "transformers": "Transformers ASR",
    "nemo": "NeMo ASR",
    "nemo-strixhalo": "NeMo ASR (Strix Halo)",
    "parakeet": "Parakeet ASR",
    "qwen3-asr": "Qwen3 ASR",
    "gemma4": "Gemma 4 ASR",
    "nemotron": "Nemotron ASR (batch + streaming)",
}

# TTS provider key (written to extras/tts/.env by init.py) → docker compose service.
_TTS_PROVIDER_TO_SERVICE = {
    "tada": "tada-tts",
    "fish_speech": "fish-tts",
    "kittentts": "kittentts-tts",
    "kokoro": "kokoro-tts",
}

# Batch ASR provider (ASR_PROVIDER) → docker compose service in extras/asr-services.
ASR_PROVIDER_TO_SERVICE = {
    "vibevoice": "vibevoice-asr",
    "vibevoice-strixhalo": "vibevoice-asr-strixhalo",
    "faster-whisper": "faster-whisper-asr",
    "transformers": "transformers-asr",
    "nemo": "nemo-asr",
    "nemo-strixhalo": "nemo-asr-strixhalo",
    "parakeet": "parakeet-asr",
    "qwen3-asr": "qwen3-asr-wrapper",
    "gemma4": "gemma4-asr",
    # Nemotron serves BOTH batch (HTTP /transcribe) and streaming (ws /stream) from
    # one container on 8772, so the batch lane reuses the streaming service.
    "nemotron": "nemotron-stream-asr",
}

# Streaming ASR provider (STREAMING_ASR_PROVIDER) options. Single source of truth
# for the System-page streaming selector: maps the provider key to its docker
# compose service (None = cloud provider, no local container), the stt_stream
# model registry entry the pipeline should use, and a UI label. A streaming
# stt_stream can run alongside a different batch stt provider (e.g. batch=vibevoice
# on 8767, streaming=nemotron on 8772) or be a pure cloud service (smallest/deepgram).
STREAMING_ASR_PROVIDER_OPTIONS = {
    "nemotron": {
        "service": "nemotron-stream-asr",
        "model": "stt-nemotron-stream",
        "label": "Nemotron 3.5 (local · 8772)",
    },
    "smallest": {
        "service": None,
        "model": "stt-smallest-stream",
        "label": "Smallest.ai PULSE (cloud)",
    },
    "deepgram": {
        "service": None,
        "model": "stt-deepgram-stream",
        "label": "Deepgram Nova 3 (cloud)",
    },
    "qwen3-asr": {
        "service": "qwen3-asr-bridge",
        "model": "stt-qwen3-asr-stream",
        "label": "Qwen3-ASR (local)",
    },
}

# Provider key → docker compose service for streaming providers that run a local
# container (cloud providers are absent). Derived from the options above so the
# start logic and the UI selector never drift.
STREAMING_ASR_PROVIDER_TO_SERVICE = {
    key: opt["service"]
    for key, opt in STREAMING_ASR_PROVIDER_OPTIONS.items()
    if opt["service"]
}


def active_streaming_asr_provider() -> str:
    """Return the provider key whose stt_stream model is the active default.

    Reads ``defaults.stt_stream`` from config.yml (the real pipeline source of
    truth — cloud providers leave STREAMING_ASR_PROVIDER empty) and reverse-maps
    it through STREAMING_ASR_PROVIDER_OPTIONS. Empty string if unset/unknown.
    """
    try:
        cfg = yaml.safe_load(
            (Path(__file__).parent / "config" / "config.yml").read_text()
        )
        model = (cfg.get("defaults") or {}).get("stt_stream")
    except (OSError, yaml.YAMLError):
        return ""
    for key, opt in STREAMING_ASR_PROVIDER_OPTIONS.items():
        if opt["model"] == model:
            return key
    return ""


def _asr_health_port(env_values: dict, default_port) -> str:
    """Resolve the port the active ASR provider actually serves health on.

    Most providers bind ASR_PORT (8767). Nemotron is special: when it's the
    batch provider it serves both batch + streaming from the nemotron-stream-asr
    container, which binds NEMOTRON_STREAM_PORT (8772) — NOT ASR_PORT. Probing
    ASR_PORT there leaves the service stuck "starting" forever.
    """
    provider = (env_values.get("ASR_PROVIDER") or "").strip("'\"")
    if provider == "nemotron":
        return str(env_values.get("NEMOTRON_STREAM_PORT", "8772")).strip("'\"")
    return str(env_values.get("ASR_PORT", default_port)).strip("'\"")


def _get_advertised_services() -> list[tuple[str, int, str]]:
    """Return list of (discovery_name, port, label) for configured services."""
    triples: list[tuple[str, int, str]] = []
    for svc_name, discovery_name in _DISCOVERY_NAMES.items():
        if svc_name not in SERVICES or not check_service_enabled(svc_name):
            continue
        service = SERVICES[svc_name]
        endpoints = service.get("health_endpoints", [])
        if not endpoints:
            continue
        _label, port_env, default_port, _path = endpoints[0]
        if port_env:
            env_path = Path(service["path"]) / ".env"
            env_values = dotenv_values(env_path) if env_path.exists() else {}
            if svc_name == "asr-services" and port_env == "ASR_PORT":
                port = int(_asr_health_port(env_values, default_port))
            else:
                port = int(env_values.get(port_env, default_port))
        else:
            port = int(default_port)

        # Derive display label
        if svc_name == "asr-services":
            env_path = Path(service["path"]) / ".env"
            asr_provider = ""
            if env_path.exists():
                asr_provider = (
                    dotenv_values(env_path).get("ASR_PROVIDER", "").strip("'\"")
                )
            label = _ASR_PROVIDER_LABELS.get(asr_provider, service["description"])
        else:
            label = service["description"]

        triples.append((discovery_name, port, label))
    return triples


def _write_advertised_services(triples: list[tuple[str, int, str]]) -> None:
    """Write advertised services to config/advertised-services.json for the backend."""
    data = [
        {"name": name, "port": port, "label": label} for name, port, label in triples
    ]
    _ADVERTISED_SERVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ADVERTISED_SERVICES_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _remove_advertised_services() -> None:
    """Remove advertised-services.json when discovery agent stops."""
    _ADVERTISED_SERVICES_PATH.unlink(missing_ok=True)


def _get_backend_env_path() -> Path:
    return Path(__file__).parent / "backends" / "advanced" / ".env"


def _langfuse_enabled_in_backend() -> bool:
    """Check if backend is configured to send traces to LangFuse."""
    backend_env_path = _get_backend_env_path()
    return all(
        read_env_value(backend_env_path, key)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
    )


def _langfuse_is_external() -> bool:
    """Check if backend is configured for an external LangFuse (not local docker)."""
    backend_env_path = _get_backend_env_path()
    host = read_env_value(backend_env_path, "LANGFUSE_HOST")
    return bool(host) and "langfuse-web" not in host


def _ensure_langfuse_env() -> bool:
    """Ensure extras/langfuse/.env exists when backend enables LangFuse."""
    service = SERVICES["langfuse"]
    service_path = Path(service["path"])
    env_path = service_path / ".env"

    if env_path.exists():
        return True

    backend_env_path = _get_backend_env_path()
    if not _langfuse_enabled_in_backend():
        console.print(
            "[yellow]⚠️  LangFuse is enabled in services list but backend is not "
            "configured for LangFuse. Skipping.[/yellow]"
        )
        return False

    if _langfuse_is_external():
        console.print(
            "[blue]ℹ️  Backend is configured for an external LangFuse instance. "
            "Local LangFuse service not needed.[/blue]"
        )
        return False

    console.print(
        "[blue]ℹ️  LangFuse enabled in backend but extras/langfuse/.env is missing. "
        "Running LangFuse init...[/blue]"
    )

    cmd = ["uv", "run", "python3", "init.py"]
    admin_email = read_env_value(backend_env_path, "ADMIN_EMAIL") or ""
    admin_password = read_env_value(backend_env_path, "ADMIN_PASSWORD") or ""
    if admin_email:
        cmd.extend(["--admin-email", admin_email])
    if admin_password:
        cmd.extend(["--admin-password", admin_password])

    try:
        result = subprocess.run(cmd, cwd=service_path)
        if result.returncode != 0:
            console.print("[red]❌ LangFuse init failed[/red]")
            return False
    except Exception as e:
        console.print(f"[red]❌ LangFuse init error: {e}[/red]")
        return False

    if not env_path.exists():
        console.print("[red]❌ LangFuse .env not created; cannot start service[/red]")
        return False

    return True


def check_service_enabled(service_name):
    """Whether a service is enabled for the lifecycle.

    Source of truth is the ``services:`` section of config/config.yml (written by
    the wizard). This is intentionally decoupled from whether a service's ``.env``
    exists — a stale or half-written ``.env`` no longer counts as "configured".
    """
    config = load_config_yml() or {}
    return bool(config.get("services", {}).get(service_name, False))


def check_service_health(service_name):
    """Check runtime health of a service by hitting its health endpoints.

    Returns (status, detail) where status is one of:
        "healthy"  — all endpoints responding with < 400
        "partial"  — some endpoints down (detail says which)
        "unhealthy" — responding but returning errors
        "stopped"  — not reachable at all
    """
    import requests

    service = SERVICES[service_name]
    endpoints = service.get("health_endpoints", [])
    if not endpoints:
        return ("stopped", "no endpoints defined")

    env_path = Path(service["path"]) / ".env"
    env_values = dotenv_values(env_path) if env_path.exists() else {}

    results = []  # list of (label, ok: bool)
    any_unhealthy = False

    for label, port_env, default_port, path in endpoints:
        if service_name == "asr-services" and port_env == "ASR_PORT":
            port = _asr_health_port(env_values, default_port)
        elif port_env:
            port = env_values.get(port_env, default_port)
        else:
            port = default_port
        url = f"http://localhost:{port}{path}"
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code < 400:
                results.append((label, True))
            else:
                results.append((label, False))
                any_unhealthy = True
        except (requests.ConnectionError, requests.Timeout):
            results.append((label, False))

    up = [r for r in results if r[1]]
    down = [r for r in results if not r[1]]

    if len(up) == len(results):
        return ("healthy", "")
    if len(down) == len(results):
        if any_unhealthy:
            return ("unhealthy", "")
        return ("stopped", "")
    down_labels = ", ".join(r[0] for r in down)
    return ("partial", f"{down_labels} down")


# Profile-gated services in the backend compose. `https` (caddy) is auto-enabled
# from the Caddyfile; the rest are opt-in via the backend BACKEND_PROFILES env var.
_BACKEND_ALL_PROFILES = ("https", "prod", "annotation", "vault-sync", "tailscale")


def _backend_profile_flags(service_path, command):
    """Return ``--profile`` flags for the backend compose.

    On ``down``/``status``/``restart`` we enable ALL profiles so the command covers
    every profile-gated service (caddy, webui-prod, annotation-cron,
    vault-syncthing, tailscale) — otherwise ``docker compose down`` silently leaves
    inactive-profile containers running (the stale-container bug).

    On ``up`` we only enable what's actually wanted: ``https`` when a Caddyfile is
    present (auto), plus any profiles listed in the backend's ``BACKEND_PROFILES``
    env var (comma-separated, e.g. ``prod,annotation,vault-sync``).
    """
    if command in ("down", "status", "restart"):
        profiles = list(_BACKEND_ALL_PROFILES)
    else:  # up
        profiles = []
        caddyfile = service_path / "Caddyfile"
        if caddyfile.exists() and caddyfile.is_file():
            profiles.append("https")
        env_file = service_path / ".env"
        extra = (
            dotenv_values(env_file).get("BACKEND_PROFILES", "")
            if env_file.exists()
            else ""
        ) or ""
        for name in extra.split(","):
            name = name.strip()
            if name and name not in profiles:
                profiles.append(name)

    flags = []
    for name in profiles:
        flags.extend(["--profile", name])
    return flags


def run_compose_command(service_name, command, build=False, force_recreate=False):
    """Run docker compose command for a service"""
    service = SERVICES[service_name]
    service_path = Path(service["path"])

    if not service_path.exists():
        console.print(f"[red]❌ Service directory not found: {service_path}[/red]")
        return False

    compose_file = service_path / service["compose_file"]
    if not compose_file.exists():
        console.print(f"[red]❌ Docker compose file not found: {compose_file}[/red]")
        return False

    # Step 1: If build is requested, run build separately first (no timeout for CUDA builds)
    if build and command == "up":
        # Build command - need to specify profiles for build too
        build_cmd = compose_base()

        # Add profiles to build command (needed for profile-specific services)
        if service_name == "backend":
            build_cmd.extend(_backend_profile_flags(service_path, "up"))

        elif service_name == "speaker-recognition":
            env_file = service_path / ".env"
            if env_file.exists():
                env_values = dotenv_values(env_file)
                # Derive profile from PYTORCH_CUDA_VERSION
                pytorch_version = env_values.get("PYTORCH_CUDA_VERSION", "cpu")
                if pytorch_version == "strixhalo":
                    profile = "strixhalo"
                elif pytorch_version.startswith("cu"):
                    profile = "gpu"
                else:
                    profile = "cpu"
                build_cmd.extend(["--profile", profile])

        # For asr-services, only build the selected provider(s)
        asr_service_to_build = None
        streaming_asr_service_to_build = None
        if service_name == "asr-services":
            env_file = service_path / ".env"
            if env_file.exists():
                env_values = dotenv_values(env_file)
                asr_provider = env_values.get("ASR_PROVIDER", "").strip("'\"")
                streaming_asr_provider = env_values.get(
                    "STREAMING_ASR_PROVIDER", ""
                ).strip("'\"")

                asr_service_to_build = ASR_PROVIDER_TO_SERVICE.get(asr_provider)
                streaming_asr_service_to_build = STREAMING_ASR_PROVIDER_TO_SERVICE.get(
                    streaming_asr_provider
                )

                if asr_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building ASR provider: {asr_provider} ({asr_service_to_build})[/blue]"
                    )
                if streaming_asr_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building streaming ASR provider: {streaming_asr_provider} ({streaming_asr_service_to_build})[/blue]"
                    )

        # For tts, only build the selected provider (one runs at a time)
        tts_service_to_build = None
        if service_name == "tts":
            env_file = service_path / ".env"
            if env_file.exists():
                tts_provider = (
                    dotenv_values(env_file).get("TTS_PROVIDER", "").strip("'\"")
                )
                tts_service_to_build = _TTS_PROVIDER_TO_SERVICE.get(tts_provider)
                if tts_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building TTS provider: {tts_provider} ({tts_service_to_build})[/blue]"
                    )

        build_cmd.append("build")

        # If building ASR, only build the specific service(s)
        if asr_service_to_build:
            if asr_provider == "qwen3-asr":
                # Qwen3-ASR also needs the streaming bridge built
                build_cmd.extend([asr_service_to_build, "qwen3-asr-bridge"])
            else:
                build_cmd.append(asr_service_to_build)
        if streaming_asr_service_to_build:
            build_cmd.append(streaming_asr_service_to_build)

        # If building TTS, only build the selected provider
        if tts_service_to_build:
            build_cmd.append(tts_service_to_build)

        # Run build with streaming output (no timeout)
        console.print(
            f"[cyan]🔨 Building {service_name} (this may take several minutes for CUDA/GPU builds)...[/cyan]"
        )
        try:
            process = subprocess.Popen(
                build_cmd,
                cwd=service_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            if process.stdout is None:
                raise RuntimeError(
                    "Process stdout is None - unable to read command output"
                )

            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                # escape() the raw build line so brackets in build output
                # (e.g. "COPY ... [/app/.venv ...]") aren't parsed as rich markup,
                # which previously raised MarkupError and aborted the build.
                safe = escape(line)
                if "error" in line.lower() or "failed" in line.lower():
                    console.print(f"  [red]{safe}[/red]")
                elif "Successfully" in line or "built" in line.lower():
                    console.print(f"  [green]{safe}[/green]")
                elif "Building" in line or "Step" in line:
                    console.print(f"  [cyan]{safe}[/cyan]")
                elif "warning" in line.lower():
                    console.print(f"  [yellow]{safe}[/yellow]")
                else:
                    console.print(f"  [dim]{safe}[/dim]")

            process.wait()

            if process.returncode != 0:
                console.print(f"\n[red]❌ Build failed for {service_name}[/red]")
                return False

            console.print(f"[green]✅ Build completed for {service_name}[/green]")

        except Exception as e:
            console.print(f"[red]❌ Error building {service_name}: {e}[/red]")
            return False

    # Step 2: Run the actual command (up/down/restart/status)
    up_flags = ["up", "-d", "--remove-orphans"]
    if force_recreate:
        up_flags.append("--force-recreate")

    cmd = compose_base()

    # Add profiles for backend service (down/status/restart cover ALL profiles so no
    # profile-gated container is left orphaned; up only enables wanted profiles).
    if service_name == "backend":
        cmd.extend(_backend_profile_flags(service_path, command))

        caddyfile_path = service_path / "Caddyfile"
        if command == "up" and caddyfile_path.exists() and caddyfile_path.is_file():
            # Only the "static" cert mode keeps a host-issued cert file that we must
            # renew. In "caddy" mode Caddy obtains and auto-renews the cert itself, so
            # we leave it alone. Renew (if missing/near expiry) before Caddy starts so
            # it comes up holding a fresh cert. Cheap no-op when still valid; never
            # blocks startup on failure.
            cert_mode = read_env_value(str(service_path / ".env"), "HTTPS_CERT_MODE")
            if cert_mode == "static":
                certs_dir = Path(__file__).parent / "certs"
                renewed = ensure_tailscale_cert(str(certs_dir))
                if renewed is True:
                    console.print("[green]✅ Renewed Tailscale TLS certificate[/green]")
                elif renewed is False:
                    console.print(
                        "[yellow]⚠️  TLS cert is near expiry but renewal failed "
                        "(Tailscale unreachable or cert issuance error). Starting with "
                        "the existing cert; HTTPS clients may see warnings.[/yellow]"
                    )

    # Handle speaker-recognition service specially
    if service_name == "speaker-recognition" and command in ["up", "down"]:
        env_file = service_path / ".env"
        if env_file.exists():
            env_values = dotenv_values(env_file)
            # Derive profile from PYTORCH_CUDA_VERSION
            pytorch_version = env_values.get("PYTORCH_CUDA_VERSION", "cpu")
            if pytorch_version == "strixhalo":
                profile = "strixhalo"
            elif pytorch_version.startswith("cu"):
                profile = "gpu"
            else:
                profile = "cpu"

            cmd.extend(["--profile", profile])

            if command == "up":
                caddyfile_path = service_path / "Caddyfile"
                https_enabled = caddyfile_path.exists() and caddyfile_path.is_file()
                if https_enabled:
                    cmd.extend(up_flags)
                else:
                    profile_to_service = {
                        "gpu": "speaker-service-gpu",
                        "strixhalo": "speaker-service-strixhalo",
                        "cpu": "speaker-service",
                    }
                    cmd.extend(
                        up_flags
                        + [profile_to_service.get(profile, "speaker-service"), "web-ui"]
                    )
            elif command == "down":
                cmd.extend(["down"])
        else:
            if command == "up":
                cmd.extend(up_flags)
            elif command == "down":
                cmd.extend(["down"])

    # Handle asr-services - start only the configured provider(s)
    elif service_name == "asr-services" and command in ["up", "down", "restart"]:
        env_file = service_path / ".env"
        asr_service_name = None
        streaming_asr_service_name = None
        asr_provider = ""

        if env_file.exists():
            env_values = dotenv_values(env_file)
            asr_provider = env_values.get("ASR_PROVIDER", "").strip("'\"")
            streaming_asr_provider = env_values.get("STREAMING_ASR_PROVIDER", "").strip(
                "'\""
            )

            asr_service_name = ASR_PROVIDER_TO_SERVICE.get(asr_provider)
            streaming_asr_service_name = STREAMING_ASR_PROVIDER_TO_SERVICE.get(
                streaming_asr_provider
            )

            if asr_service_name:
                console.print(
                    f"[blue]ℹ️  Using ASR provider: {asr_provider} ({asr_service_name})[/blue]"
                )
            if streaming_asr_service_name:
                console.print(
                    f"[blue]ℹ️  Using streaming ASR provider: {streaming_asr_provider} ({streaming_asr_service_name})[/blue]"
                )

        if command == "up":
            if asr_service_name:
                services_to_start = [asr_service_name]
                # Qwen3-ASR also needs the streaming bridge
                if asr_provider == "qwen3-asr":
                    services_to_start.append("qwen3-asr-bridge")
            else:
                console.print(
                    "[yellow]⚠️  No ASR_PROVIDER configured, starting default service[/yellow]"
                )
                services_to_start = ["vibevoice-asr"]
            if streaming_asr_service_name:
                services_to_start.append(streaming_asr_service_name)
            cmd.extend(up_flags + services_to_start)
        elif command == "down":
            cmd.extend(["down"])
        elif command == "restart":
            if asr_service_name:
                services_to_restart = [asr_service_name]
                if asr_provider == "qwen3-asr":
                    services_to_restart.append("qwen3-asr-bridge")
                if streaming_asr_service_name:
                    services_to_restart.append(streaming_asr_service_name)
                cmd.extend(["restart"] + services_to_restart)
            else:
                cmd.extend(["restart"])

    # Handle tts - start only the configured provider (one runs at a time, port 8770)
    elif service_name == "tts" and command in ["up", "down", "restart"]:
        env_file = service_path / ".env"
        tts_service_name = None
        if env_file.exists():
            tts_provider = dotenv_values(env_file).get("TTS_PROVIDER", "").strip("'\"")
            tts_service_name = _TTS_PROVIDER_TO_SERVICE.get(tts_provider)
            if tts_service_name:
                console.print(
                    f"[blue]ℹ️  Using TTS provider: {tts_provider} ({tts_service_name})[/blue]"
                )

        if command == "up":
            if tts_service_name:
                cmd.extend(up_flags + [tts_service_name])
            else:
                console.print(
                    "[yellow]⚠️  No TTS_PROVIDER configured; run extras/tts/init.py[/yellow]"
                )
                cmd.extend(up_flags + ["kittentts-tts"])
        elif command == "down":
            # Plain down removes every tts container regardless of provider.
            cmd.extend(["down"])
        elif command == "restart":
            cmd.extend(["restart"] + ([tts_service_name] if tts_service_name else []))

    else:
        # Standard compose commands for other services
        if command == "up":
            cmd.extend(up_flags)
        elif command == "down":
            cmd.extend(["down"])
        elif command == "restart":
            cmd.extend(["restart"])
        elif command == "status":
            cmd.extend(["ps"])

    try:
        # Run the command with timeout (build already done if needed)
        result = subprocess.run(
            cmd,
            cwd=service_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,  # 2 minute timeout
        )

        if result.returncode == 0:
            return True
        else:
            console.print(f"[red]❌ Command failed[/red]")
            if result.stderr:
                console.print("[red]Error output:[/red]")
                for line in result.stderr.splitlines():
                    console.print(f"  [dim]{escape(line)}[/dim]")
            return False

    except subprocess.TimeoutExpired:
        console.print(
            f"[red]❌ Command timed out after 2 minutes for {service_name}[/red]"
        )
        return False
    except Exception as e:
        console.print(f"[red]❌ Error running command: {e}[/red]")
        return False


def ensure_docker_network():
    """Ensure chronicle-network exists"""
    try:
        # Check if network already exists
        engine = container_engine()
        result = subprocess.run(
            [engine, "network", "inspect", "chronicle-network"],
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            # Network doesn't exist, create it
            console.print("[blue]📡 Creating chronicle-network...[/blue]")
            subprocess.run(
                [engine, "network", "create", "chronicle-network"],
                check=True,
                capture_output=True,
            )
            console.print("[green]✅ chronicle-network created[/green]")
        else:
            console.print("[dim]📡 chronicle-network already exists[/dim]")
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to create network: {e}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]❌ Error checking/creating network: {e}[/red]")
        return False


# --- Service manager agent (native process, not Docker) ---
# Host-side HTTP API (edge/service_manager.py) that lets the backend (and thus
# the WebUI System page) start/stop/restart services and switch ASR/TTS
# providers. Runs natively because docker compose needs host bind-mount paths.

_SERVICE_MANAGER_PID = Path(__file__).parent / "edge" / ".service-manager.pid"
_SERVICE_MANAGER_LOG = Path(__file__).parent / "edge" / "service-manager.log"
_SERVICE_MANAGER_PORT = "8775"


def _service_manager_running() -> bool:
    """Check if service manager agent process is alive."""
    if _service_manager_managed():
        return _unit_active("chronicle-service-manager")
    if not _SERVICE_MANAGER_PID.exists():
        return False
    try:
        pid = int(_SERVICE_MANAGER_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        _SERVICE_MANAGER_PID.unlink(missing_ok=True)
        return False


def _ensure_service_manager_token() -> str:
    """Read SERVICE_MANAGER_TOKEN from backend .env, generating it on first use.

    The backend .env is the single source of truth: the backend container gets
    the token via env_file, and the agent gets it from here at launch.
    """
    backend_env_path = _get_backend_env_path()
    token = read_env_value(backend_env_path, "SERVICE_MANAGER_TOKEN") or ""
    if not token:
        token = secrets.token_hex(24)
        backend_env_path.touch(exist_ok=True)
        set_key(
            str(backend_env_path), "SERVICE_MANAGER_TOKEN", token, quote_mode="never"
        )
        console.print(
            "[blue]ℹ️  Generated SERVICE_MANAGER_TOKEN in backends/advanced/.env[/blue]"
        )
    return token


def _start_service_manager():
    """Start the service manager agent as a native background process."""
    # Heal legacy installs: the old standalone discovery agent is now folded in.
    _cleanup_legacy_discovery()
    if _service_manager_managed():
        _systemctl_user("start", "chronicle-service-manager", capture=False)
        console.print(
            "[dim]🛠  Service manager managed by systemd (ensured started)[/dim]"
        )
        return True
    if _service_manager_running():
        console.print("[dim]🛠  Service manager already running[/dim]")
        return True

    agent_script = Path(__file__).parent / "edge" / "service_manager.py"
    if not agent_script.exists():
        console.print("[yellow]⚠️  edge/service_manager.py not found, skipping[/yellow]")
        return False

    env = dict(os.environ)
    env["SERVICE_MANAGER_TOKEN"] = _ensure_service_manager_token()
    env.setdefault("SERVICE_MANAGER_PORT", _SERVICE_MANAGER_PORT)

    log_file = open(_SERVICE_MANAGER_LOG, "a")
    try:
        proc = subprocess.Popen(
            [
                "uv",
                "run",
                "--with-requirements",
                "setup-requirements.txt",
                "--with",
                "fastapi",
                "--with",
                "uvicorn",
                "python",
                str(agent_script),
            ],
            cwd=Path(__file__).parent,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        console.print(f"[red]❌ Failed to start service manager: {e}[/red]")
        log_file.close()
        return False

    log_file.close()
    _SERVICE_MANAGER_PID.write_text(str(proc.pid))
    console.print(
        f"[green]✅ Service manager started (PID {proc.pid}, port {env['SERVICE_MANAGER_PORT']})[/green]"
    )
    return True


def _stop_service_manager():
    """Stop the service manager agent process."""
    if _service_manager_managed():
        console.print(
            "[dim]🛠  Service manager is a systemd service — left running "
            "(use 'systemctl --user stop chronicle-service-manager' to stop)[/dim]"
        )
        return
    if not _SERVICE_MANAGER_PID.exists():
        _remove_advertised_services()
        return

    try:
        pid = int(_SERVICE_MANAGER_PID.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        console.print(f"[green]✅ Service manager stopped (PID {pid})[/green]")
    except (ValueError, OSError):
        console.print("[dim]Service manager already stopped[/dim]")
    finally:
        _SERVICE_MANAGER_PID.unlink(missing_ok=True)
        # The node agent was the advertiser — clear the local manifest on stop.
        _remove_advertised_services()


# --- systemd user-service integration (auto-start agents on boot) ---
#
# The service manager and discovery agents are native host processes, not
# containers, so a plain ``./start.sh`` launch does not survive a reboot the way
# Docker's restart policy revives the containers. Optionally install them as
# systemd *user* services (with linger enabled) so they come back on boot. The
# unit's ExecStart re-invokes ``services.py <agent> run`` — a foreground runner
# that reuses the same token/advertise setup and then ``exec``s the agent, so
# systemd tracks the agent process directly.

_REPO_ROOT = Path(__file__).resolve().parent
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

# unit name -> (services.py subcommand for ExecStart, human description)
_SYSTEMD_UNITS = {
    "chronicle-service-manager": (
        "manager run",
        "Chronicle node agent (WebUI control + Tailnet service advertising)",
    ),
}

_SYSTEMD_STATES = {
    "running",
    "degraded",
    "starting",
    "initializing",
    "maintenance",
    "stopping",
}


def _systemctl_user(*args, capture=True):
    """Run ``systemctl --user`` and return the CompletedProcess."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=capture,
        text=True,
    )


def _systemd_user_available() -> bool:
    """True if a systemd *user* instance is reachable.

    Not the case on hosts without systemd, or on WSL without ``systemd=true``
    in /etc/wsl.conf (the user bus is then unavailable).
    """
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (result.stdout or "").strip() in _SYSTEMD_STATES


def _unit_enabled(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl_user("is-enabled", unit).returncode == 0


def _unit_active(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl_user("is-active", unit).returncode == 0


def _service_manager_managed() -> bool:
    return _unit_enabled("chronicle-service-manager")


def _write_systemd_unit(unit: str) -> Path:
    subcmd, description = _SYSTEMD_UNITS[unit]
    uv_path = shutil.which("uv") or "uv"
    # A systemd user unit gets a minimal PATH (no ~/.local/bin), so neither uv
    # nor binaries the agents shell out to (e.g. tailscale) would resolve.
    # Pin a sane PATH that includes uv's own directory.
    uv_dir = str(Path(uv_path).parent)
    unit_path_env = ":".join(
        [
            uv_dir,
            str(Path.home() / ".local" / "bin"),
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
    )
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = _SYSTEMD_USER_DIR / f"{unit}.service"
    unit_path.write_text(
        f"""[Unit]
Description={description}

[Service]
Type=exec
WorkingDirectory={_REPO_ROOT}
Environment=PATH={unit_path_env}
ExecStart={uv_path} run --with-requirements setup-requirements.txt python {_REPO_ROOT / "services.py"} {subcmd}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    )
    return unit_path


def _install_systemd_unit(unit: str) -> bool:
    """Write, enable and start a single user unit. Assumes systemd is available."""
    # Stop any background (Popen) instance first so the unit can bind the port.
    if unit == "chronicle-service-manager":
        _stop_service_manager()
        _cleanup_legacy_discovery()

    path = _write_systemd_unit(unit)
    # Keep the user instance (and thus the agent) alive after logout / reboot.
    subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True)
    _systemctl_user("daemon-reload")
    result = _systemctl_user("enable", "--now", unit, capture=False)
    if result.returncode != 0:
        console.print(f"[red]❌ Failed to enable {unit}[/red]")
        return False
    console.print(
        f"[green]✅ Installed & started {unit} (systemd user service)[/green]"
    )
    console.print(f"[dim]   Unit: {path}[/dim]")
    return True


def _uninstall_systemd_unit(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        console.print("[dim]systemctl not found — nothing to uninstall[/dim]")
        return False
    _systemctl_user("disable", "--now", unit, capture=False)
    (_SYSTEMD_USER_DIR / f"{unit}.service").unlink(missing_ok=True)
    _systemctl_user("daemon-reload")
    console.print(f"[green]✅ Removed {unit} systemd user service[/green]")
    return True


def _print_systemd_unavailable_help():
    console.print(
        "[yellow]⚠️  No systemd user instance available — cannot install services.[/yellow]"
    )
    console.print(
        "[dim]   On WSL, enable it: add a '[boot]' section with 'systemd=true' to "
        "/etc/wsl.conf, run 'wsl --shutdown', then reopen the terminal.[/dim]"
    )


def install_systemd_agents(units=None) -> bool:
    """Install the given agent units (default: all) as systemd user services."""
    units = units or list(_SYSTEMD_UNITS)
    if not _systemd_user_available():
        _print_systemd_unavailable_help()
        return False
    ok = True
    for unit in units:
        ok = _install_systemd_unit(unit) and ok
    if ok:
        console.print(
            "[dim]These agents will now auto-start on boot. Manage them with "
            "'systemctl --user status/stop/restart <unit>'.[/dim]"
        )
    return ok


def uninstall_systemd_agents(units=None) -> bool:
    units = units or list(_SYSTEMD_UNITS)
    ok = True
    for unit in units:
        ok = _uninstall_systemd_unit(unit) and ok
    return ok


def _cleanup_legacy_discovery() -> None:
    """Remove the old standalone discovery agent, now folded into the node agent.

    Older installs ran a separate ``chronicle-discovery`` systemd user unit and/or
    a native ``edge/.discovery-agent.pid`` process. The node agent (service
    manager) now does the advertising, so a leftover discovery agent would
    double-advertise — and its unit's ExecStart (``services.py discovery run``) no
    longer exists, so it would crash-loop. Disable + remove it. Idempotent.
    """
    unit = "chronicle-discovery"
    unit_file = _SYSTEMD_USER_DIR / f"{unit}.service"
    if shutil.which("systemctl") and (_unit_enabled(unit) or unit_file.exists()):
        _systemctl_user("disable", "--now", unit)
        unit_file.unlink(missing_ok=True)
        _systemctl_user("daemon-reload")
        console.print("[dim]🧹 Removed legacy chronicle-discovery systemd unit[/dim]")

    pid_file = Path(__file__).parent / "edge" / ".discovery-agent.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(pid, signal.SIGTERM)
            console.print(f"[dim]🧹 Stopped legacy discovery agent (PID {pid})[/dim]")
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)


def _service_manager_exec():
    """Foreground exec of the service manager agent (for systemd ExecStart)."""
    agent_script = _REPO_ROOT / "edge" / "service_manager.py"
    if not agent_script.exists():
        console.print("[red]❌ edge/service_manager.py not found[/red]")
        sys.exit(1)
    os.environ["SERVICE_MANAGER_TOKEN"] = _ensure_service_manager_token()
    os.environ.setdefault("SERVICE_MANAGER_PORT", _SERVICE_MANAGER_PORT)
    os.chdir(_REPO_ROOT)
    # Absolute uv path: a systemd user unit's PATH does not include ~/.local/bin.
    uv = shutil.which("uv") or "uv"
    os.execv(
        uv,
        [
            uv,
            "run",
            "--with-requirements",
            "setup-requirements.txt",
            "--with",
            "fastapi",
            "--with",
            "uvicorn",
            "python",
            str(agent_script),
        ],
    )


# --- Claude remote-control session (control Claude Code from your phone) ---
#
# `claude remote-control` runs a persistent server that accepts multiple sessions
# you spawn from the Claude mobile app / claude.ai/code. It is an interactive TUI,
# so it needs a pty — we run it inside a detached tmux session (also lets you
# `tmux attach -t chronicle-rc` at the desktop). Persistence is via a systemd user
# unit (tmux oneshot) so it survives reboot, with a WebUI start/stop toggle.

_CLAUDE_RC_UNIT = "chronicle-remote-control"
_CLAUDE_RC_SESSION = os.environ.get("CLAUDE_RC_SESSION", "chronicle-rc")


def _claude_rc_dir() -> Path:
    """Directory the remote-control server (and its spawned sessions) runs in."""
    return Path(os.environ.get("CLAUDE_RC_DIR") or _REPO_ROOT)


def _claude_rc_name() -> str:
    """Session name shown in claude.ai/code (defaults to the hostname)."""
    return os.environ.get("CLAUDE_RC_NAME") or os.uname().nodename


def _claude_rc_command() -> list[str]:
    """The `claude remote-control` argv (same-dir spawn, default permission mode)."""
    claude = shutil.which("claude") or "claude"
    return [
        claude,
        "remote-control",
        "--spawn=same-dir",
        "--permission-mode=default",
        "--name",
        _claude_rc_name(),
    ]


def _tmux_session_running(session: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _remote_control_managed() -> bool:
    return _unit_enabled(_CLAUDE_RC_UNIT)


def remote_control_status() -> dict:
    """Current state of the Claude remote-control session."""
    return {
        "running": _tmux_session_running(_CLAUDE_RC_SESSION),
        "managed": _remote_control_managed(),
        "session": _CLAUDE_RC_SESSION,
        "dir": str(_claude_rc_dir()),
        "name": _claude_rc_name(),
        "tmux_available": shutil.which("tmux") is not None,
        "claude_available": shutil.which("claude") is not None,
    }


def _start_remote_control_tmux() -> bool:
    """Launch the remote-control server in a detached tmux session (idempotent)."""
    if shutil.which("tmux") is None:
        console.print(
            "[red]❌ tmux not found — install tmux to run remote-control[/red]"
        )
        return False
    if shutil.which("claude") is None:
        console.print(
            "[red]❌ claude CLI not found — install Claude Code and log in first[/red]"
        )
        return False
    if _tmux_session_running(_CLAUDE_RC_SESSION):
        console.print(
            f"[dim]📱 Claude remote-control already running (tmux: {_CLAUDE_RC_SESSION})[/dim]"
        )
        return True
    rc_dir = _claude_rc_dir()
    # Pass the claude command as one string so tmux's shell runs it; when it exits
    # the session ends, so `has-session` faithfully reflects whether it is alive.
    cmd = " ".join(shlex.quote(part) for part in _claude_rc_command())
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", _CLAUDE_RC_SESSION, "-c", str(rc_dir), cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(
            f"[red]❌ Failed to start remote-control: {result.stderr.strip()}[/red]"
        )
        return False
    console.print(
        f"[green]✅ Claude remote-control started in {rc_dir} "
        f"(tmux: {_CLAUDE_RC_SESSION}, name: {_claude_rc_name()})[/green]"
    )
    console.print(
        "[dim]   Spawn sessions from the Claude mobile app / claude.ai/code → Code tab.[/dim]"
    )
    return True


def _stop_remote_control_tmux() -> bool:
    if not _tmux_session_running(_CLAUDE_RC_SESSION):
        console.print("[dim]📱 Claude remote-control already stopped[/dim]")
        return True
    subprocess.run(
        ["tmux", "kill-session", "-t", _CLAUDE_RC_SESSION],
        capture_output=True,
        text=True,
    )
    console.print("[green]✅ Claude remote-control stopped[/green]")
    return True


def start_remote_control() -> bool:
    """Start remote-control, deferring to systemd when it manages the unit."""
    if _remote_control_managed():
        _systemctl_user("start", _CLAUDE_RC_UNIT, capture=False)
        console.print(
            "[dim]📱 Remote-control managed by systemd (ensured started)[/dim]"
        )
        return True
    return _start_remote_control_tmux()


def stop_remote_control() -> bool:
    if _remote_control_managed():
        _systemctl_user("stop", _CLAUDE_RC_UNIT, capture=False)
        console.print("[dim]📱 Remote-control (systemd) stopped[/dim]")
        return True
    return _stop_remote_control_tmux()


def _write_remote_control_unit() -> Path:
    """Write the chronicle-remote-control systemd user unit (tmux oneshot)."""
    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    rc_dir = _claude_rc_dir()
    cmd = " ".join(shlex.quote(part) for part in _claude_rc_command())
    uv_dir = str(Path(shutil.which("uv") or "uv").parent)
    unit_path_env = ":".join(
        [
            uv_dir,
            str(Path.home() / ".local" / "bin"),
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
    )
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = _SYSTEMD_USER_DIR / f"{_CLAUDE_RC_UNIT}.service"
    # Type=oneshot + RemainAfterExit: ExecStart spawns the detached tmux session and
    # returns; ExecStop tears it down. tmux supplies the pty the TUI needs.
    unit_path.write_text(
        f"""[Unit]
Description=Chronicle Claude remote-control session (control Claude Code from your phone)

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={rc_dir}
Environment=PATH={unit_path_env}
ExecStart={tmux} new-session -d -s {_CLAUDE_RC_SESSION} -c {rc_dir} {shlex.quote(cmd)}
ExecStop={tmux} kill-session -t {_CLAUDE_RC_SESSION}

[Install]
WantedBy=default.target
"""
    )
    return unit_path


def install_remote_control() -> bool:
    """Install + start the remote-control session as a systemd user service."""
    if shutil.which("claude") is None:
        console.print(
            "[red]❌ claude CLI not found — install Claude Code and log in first[/red]"
        )
        return False
    if shutil.which("tmux") is None:
        console.print("[red]❌ tmux not found — install tmux first[/red]")
        return False
    if not _systemd_user_available():
        _print_systemd_unavailable_help()
        return False
    # Drop any manual tmux instance so the unit owns the session name.
    _stop_remote_control_tmux()
    path = _write_remote_control_unit()
    subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True)
    _systemctl_user("daemon-reload")
    result = _systemctl_user("enable", "--now", _CLAUDE_RC_UNIT, capture=False)
    if result.returncode != 0:
        console.print(f"[red]❌ Failed to enable {_CLAUDE_RC_UNIT}[/red]")
        return False
    console.print(
        f"[green]✅ Installed & started {_CLAUDE_RC_UNIT} (systemd user service)[/green]"
    )
    console.print(f"[dim]   Unit: {path}[/dim]")
    console.print(
        f"[dim]   Sessions run in {_claude_rc_dir()}; spawn them from the Claude app "
        "(Code tab). Override dir/name with CLAUDE_RC_DIR / CLAUDE_RC_NAME.[/dim]"
    )
    return True


def uninstall_remote_control() -> bool:
    if shutil.which("systemctl") is None:
        console.print("[dim]systemctl not found — nothing to uninstall[/dim]")
        return False
    _systemctl_user("disable", "--now", _CLAUDE_RC_UNIT, capture=False)
    (_SYSTEMD_USER_DIR / f"{_CLAUDE_RC_UNIT}.service").unlink(missing_ok=True)
    _systemctl_user("daemon-reload")
    console.print(f"[green]✅ Removed {_CLAUDE_RC_UNIT} systemd user service[/green]")
    return True


def start_services(services, build=False, force_recreate=False):
    """Start specified services"""
    console.print(f"🚀 [bold]Starting {len(services)} services...[/bold]")

    # Ensure Docker network exists before starting services
    if not ensure_docker_network():
        console.print("[red]❌ Cannot start services without Docker network[/red]")
        return

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue

        if service_name == "langfuse" and not _ensure_langfuse_env():
            console.print("[yellow]⚠️  LangFuse not configured, skipping[/yellow]")
            continue

        if not check_service_enabled(service_name):
            console.print(
                f"[yellow]⚠️  {service_name} not configured, skipping[/yellow]"
            )
            continue

        console.print(f"\n🔧 Starting {service_name}...")
        if run_compose_command(service_name, "up", build, force_recreate):
            console.print(f"[green]✅ {service_name} started[/green]")
            success_count += 1
        else:
            console.print(f"[red]❌ Failed to start {service_name}[/red]")

    console.print(
        f"\n[green]🎉 {success_count}/{len(services)} services started successfully[/green]"
    )

    # Start the node agent (WebUI control + Tailnet advertising) on any start.
    # It advertises this node's enabled services regardless of whether the
    # backend runs here, so service-only nodes (e.g. a GPU/RPi box) advertise too.
    _start_service_manager()

    # Show access URLs if backend was started
    if "backend" in services and check_service_enabled("backend"):
        backend_env = _get_backend_env_path()
        https_enabled = (
            read_env_value(backend_env, "HTTPS_ENABLED") or ""
        ).lower() == "true"
        server_ip = read_env_value(backend_env, "SERVER_IP") or ""

        if https_enabled and server_ip:
            webui_url = f"https://{server_ip}"
            api_url = f"https://{server_ip}/api"
        else:
            host = server_ip or "localhost"
            webui_port = read_env_value(backend_env, "WEBUI_PORT") or "5173"
            backend_port = read_env_value(backend_env, "BACKEND_PUBLIC_PORT") or "8000"
            webui_url = f"http://{host}:{webui_port}"
            api_url = f"http://{host}:{backend_port}/api"

        console.print("")
        console.print("[bold cyan]Access URLs:[/bold cyan]")
        console.print(f"   Web Dashboard:  {webui_url}")
        console.print(f"   API:            {api_url}")

    # Show LangFuse prompt management tip if langfuse was started
    if "langfuse" in services and check_service_enabled("langfuse"):
        backend_env = _get_backend_env_path()
        langfuse_host = read_env_value(backend_env, "SERVER_IP") or "localhost"
        langfuse_url = f"http://{langfuse_host}:3002/project/chronicle/prompts"
        console.print(f"   Prompt Mgmt:    {langfuse_url}")


def stop_services(services, stop_manager=False):
    """Stop specified services.

    The service manager agent is only stopped on a full ``stop --all`` —
    otherwise it stays up so individual services can be restarted from the UI.
    """
    console.print(f"🛑 [bold]Stopping {len(services)} services...[/bold]")

    # The node agent (advertiser + control) is decoupled from the backend — it
    # only stops on a full ``stop --all`` so individual services can still be
    # restarted from the UI and advertising survives a backend-only stop.
    if stop_manager:
        _stop_service_manager()

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue

        console.print(f"\n🔧 Stopping {service_name}...")
        if run_compose_command(service_name, "down"):
            console.print(f"[green]✅ {service_name} stopped[/green]")
            success_count += 1
        else:
            console.print(f"[red]❌ Failed to stop {service_name}[/red]")

    console.print(
        f"\n[green]🎉 {success_count}/{len(services)} services stopped successfully[/green]"
    )


def restart_services(services, recreate=False):
    """Restart specified services"""
    console.print(f"🔄 [bold]Restarting {len(services)} services...[/bold]")

    if recreate:
        console.print(
            "[dim]Using down + up to recreate containers (fixes WSL2 bind mount issues)[/dim]\n"
        )
    else:
        console.print(
            "[dim]Quick restart (use --recreate to fix bind mount issues)[/dim]\n"
        )

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue

        if not check_service_enabled(service_name):
            console.print(
                f"[yellow]⚠️  {service_name} not configured, skipping[/yellow]"
            )
            continue

        console.print(f"\n🔧 Restarting {service_name}...")

        if recreate:
            # Full recreation: down + up (fixes bind mount issues)
            if not run_compose_command(service_name, "down"):
                console.print(f"[red]❌ Failed to stop {service_name}[/red]")
                continue

            if run_compose_command(service_name, "up"):
                console.print(f"[green]✅ {service_name} restarted[/green]")
                success_count += 1
            else:
                console.print(f"[red]❌ Failed to start {service_name}[/red]")
        else:
            # Quick restart: docker compose restart
            if run_compose_command(service_name, "restart"):
                console.print(f"[green]✅ {service_name} restarted[/green]")
                success_count += 1
            else:
                console.print(f"[red]❌ Failed to restart {service_name}[/red]")

    console.print(
        f"\n[green]🎉 {success_count}/{len(services)} services restarted successfully[/green]"
    )

    # Ensure the node agent (control + advertising) is running
    _start_service_manager()


def show_status():
    """Show status of all services"""
    console.print("📊 [bold]Service Status:[/bold]\n")

    table = Table()
    table.add_column("Service", style="cyan")
    table.add_column("Configured", justify="center")
    table.add_column("Running", justify="center")
    table.add_column("Description", style="dim")
    table.add_column("Ports", style="green")

    for service_name, service_info in SERVICES.items():
        configured = "✅" if check_service_enabled(service_name) else "❌"
        ports = ", ".join(service_info["ports"])

        # Check runtime health
        status, detail = check_service_health(service_name)
        if status == "healthy":
            running = "[green]✅ healthy[/green]"
        elif status == "partial":
            running = f"[yellow]⚠ partial[/yellow] [dim]({detail})[/dim]"
        elif status == "unhealthy":
            running = "[red]⚠ unhealthy[/red]"
        else:
            running = "[dim]— stopped[/dim]"

        table.add_row(
            service_name, configured, running, service_info["description"], ports
        )

    console.print(table)

    # Node agent status (control + Tailnet advertising)
    if _service_manager_managed():
        state = "running" if _unit_active("chronicle-service-manager") else "stopped"
        console.print(
            f"[green]🛠  Service manager {state} (systemd user service, port {_SERVICE_MANAGER_PORT})[/green]"
        )
    elif _service_manager_running():
        pid = int(_SERVICE_MANAGER_PID.read_text().strip())
        console.print(
            f"[green]🛠  Service manager running (PID {pid}, port {_SERVICE_MANAGER_PORT})[/green]"
        )
    else:
        console.print("[dim]🛠  Service manager not running[/dim]")

    console.print("\n💡 [dim]Use './start.sh' to start all configured services[/dim]")


def main():
    parser = argparse.ArgumentParser(description="Chronicle Service Management")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start command
    start_parser = subparsers.add_parser("start", help="Start services")
    start_parser.add_argument(
        "services",
        nargs="*",
        help="Services to start: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)",
    )
    start_parser.add_argument(
        "--all", action="store_true", help="Start all configured services"
    )
    start_parser.add_argument(
        "--build", action="store_true", help="Build images before starting"
    )
    start_parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="Force recreate containers even if unchanged",
    )
    start_parser.add_argument(
        "--use-prebuilt",
        metavar="TAG",
        help="Use prebuilt images from GHCR (or custom registry via CHRONICLE_REGISTRY env var)",
    )

    # Stop command
    stop_parser = subparsers.add_parser("stop", help="Stop services")
    stop_parser.add_argument(
        "services",
        nargs="*",
        help="Services to stop: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)",
    )
    stop_parser.add_argument("--all", action="store_true", help="Stop all services")

    # Restart command
    restart_parser = subparsers.add_parser("restart", help="Restart services")
    restart_parser.add_argument(
        "services",
        nargs="*",
        help="Services to restart: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)",
    )
    restart_parser.add_argument(
        "--all", action="store_true", help="Restart all services"
    )
    restart_parser.add_argument(
        "--recreate",
        action="store_true",
        help="Recreate containers (down + up) instead of quick restart - fixes WSL2 bind mount issues",
    )

    # Status command
    subparsers.add_parser("status", help="Show service status")

    # Service manager agent command
    manager_parser = subparsers.add_parser(
        "manager", help="Manage the service manager agent (WebUI start/stop control)"
    )
    manager_parser.add_argument(
        "manager_action",
        choices=["start", "stop", "restart", "run", "install", "uninstall"],
        help="Agent action ('run' = foreground, for systemd; 'install'/'uninstall' = systemd user service)",
    )

    # Claude remote-control session command
    rc_parser = subparsers.add_parser(
        "remote-control",
        help="Manage the Claude remote-control session (control Claude Code from your phone)",
    )
    rc_parser.add_argument(
        "remote_control_action",
        choices=["start", "stop", "restart", "status", "install", "uninstall"],
        help="Action ('install'/'uninstall' = systemd user service for boot persistence)",
    )

    args = parser.parse_args()

    if not args.command:
        show_status()
        return

    if args.command == "status":
        show_status()

    elif args.command == "start":
        if args.all:
            services = [
                s
                for s in SERVICES.keys()
                if check_service_enabled(s)
                or (s == "langfuse" and _langfuse_enabled_in_backend())
            ]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(
                    f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]"
                )
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print(
                "[red]❌ No services specified. Use --all or specify service names.[/red]"
            )
            return

        if args.use_prebuilt:
            if os.environ.get("CHRONICLE_REGISTRY"):
                registry = os.environ["CHRONICLE_REGISTRY"]
            elif os.environ.get("DOCKERHUB_USERNAME"):
                registry = f"{os.environ['DOCKERHUB_USERNAME']}/"
            else:
                registry = "ghcr.io/simpleopensoftware/"
            os.environ["CHRONICLE_REGISTRY"] = registry
            os.environ["CHRONICLE_TAG"] = args.use_prebuilt
            console.print(
                f"[cyan]ℹ️  Using prebuilt images: {registry}*:{args.use_prebuilt}[/cyan]"
            )
            build_flag = False
        else:
            build_flag = args.build

        start_services(services, build_flag, args.force_recreate)

    elif args.command == "stop":
        if args.all:
            # Only stop configured services (like start --all does)
            services = [s for s in SERVICES.keys() if check_service_enabled(s)]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(
                    f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]"
                )
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print(
                "[red]❌ No services specified. Use --all or specify service names.[/red]"
            )
            return

        stop_services(services, stop_manager=args.all)

    elif args.command == "restart":
        if args.all:
            services = [s for s in SERVICES.keys() if check_service_enabled(s)]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(
                    f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]"
                )
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print(
                "[red]❌ No services specified. Use --all or specify service names.[/red]"
            )
            return

        restart_services(services, recreate=args.recreate)

    elif args.command == "manager":
        if args.manager_action == "start":
            _start_service_manager()
        elif args.manager_action == "stop":
            _stop_service_manager()
        elif args.manager_action == "restart":
            _stop_service_manager()
            _start_service_manager()
        elif args.manager_action == "run":
            _service_manager_exec()
        elif args.manager_action == "install":
            install_systemd_agents(["chronicle-service-manager"])
        elif args.manager_action == "uninstall":
            uninstall_systemd_agents(["chronicle-service-manager"])

    elif args.command == "remote-control":
        if args.remote_control_action == "start":
            start_remote_control()
        elif args.remote_control_action == "stop":
            stop_remote_control()
        elif args.remote_control_action == "restart":
            stop_remote_control()
            start_remote_control()
        elif args.remote_control_action == "status":
            console.print(remote_control_status())
        elif args.remote_control_action == "install":
            install_remote_control()
        elif args.remote_control_action == "uninstall":
            uninstall_remote_control()


if __name__ == "__main__":
    main()
