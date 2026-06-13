#!/usr/bin/env python3
"""
Chronicle Service Management
Start, stop, and manage configured services
"""

import argparse
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values, set_key
from rich.console import Console
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
        "description": "Text-to-Speech (TADA / Fish Speech / KittenTTS)",
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
    "vibevoice-strixhalo": "VibeVoice ASR",
    "faster-whisper": "Faster Whisper ASR",
    "transformers": "Transformers ASR",
    "nemo": "NeMo ASR",
    "nemo-strixhalo": "NeMo ASR",
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

# Optional streaming ASR provider (STREAMING_ASR_PROVIDER) → docker compose service.
# Lets a low-latency streaming stt_stream run alongside a different batch stt
# provider (e.g. batch=vibevoice on 8767, streaming=nemotron on 8772).
STREAMING_ASR_PROVIDER_TO_SERVICE = {
    "nemotron": "nemotron-stream-asr",
    "qwen3-asr": "qwen3-asr-bridge",
}


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
            if env_path.exists():
                port = int(dotenv_values(env_path).get(port_env, default_port))
            else:
                port = int(default_port)
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


def _build_advertise_string() -> str:
    """Build ADVERTISE=name:port,... string from configured services."""
    return ",".join(
        f"{name}:{port}" for name, port, _label in _get_advertised_services()
    )


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
        port = env_values.get(port_env, default_port) if port_env else default_port
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
        build_cmd = ["docker", "compose"]

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

                if "error" in line.lower() or "failed" in line.lower():
                    console.print(f"  [red]{line}[/red]")
                elif "Successfully" in line or "built" in line.lower():
                    console.print(f"  [green]{line}[/green]")
                elif "Building" in line or "Step" in line:
                    console.print(f"  [cyan]{line}[/cyan]")
                elif "warning" in line.lower():
                    console.print(f"  [yellow]{line}[/yellow]")
                else:
                    console.print(f"  [dim]{line}[/dim]")

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

    cmd = ["docker", "compose"]

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
                    console.print(f"  [dim]{line}[/dim]")
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
        result = subprocess.run(
            ["docker", "network", "inspect", "chronicle-network"],
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            # Network doesn't exist, create it
            console.print("[blue]📡 Creating chronicle-network...[/blue]")
            subprocess.run(
                ["docker", "network", "create", "chronicle-network"],
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


# --- Discovery agent (native process, not Docker) ---

_DISCOVERY_PID = Path(__file__).parent / "edge" / ".discovery-agent.pid"
_DISCOVERY_LOG = Path(__file__).parent / "edge" / "discovery-agent.log"


def _discovery_agent_running() -> bool:
    """Check if discovery agent process is alive."""
    if not _DISCOVERY_PID.exists():
        return False
    try:
        pid = int(_DISCOVERY_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        _DISCOVERY_PID.unlink(missing_ok=True)
        return False


def _start_discovery_agent():
    """Start discovery agent as a native background process.

    Runs outside Docker so it can bind to the Tailscale interface directly
    (Docker Desktop VMs cannot see tailscale0).
    """
    if _discovery_agent_running():
        console.print("[dim]📡 Discovery agent already running[/dim]")
        return True

    agent_script = Path(__file__).parent / "edge" / "agent.py"
    if not agent_script.exists():
        console.print("[yellow]⚠️  edge/agent.py not found, skipping discovery[/yellow]")
        return False

    pairs = _get_advertised_services()
    if not pairs:
        console.print("[dim]📡 No services to advertise, skipping discovery[/dim]")
        return False

    _write_advertised_services(pairs)
    advertise = ",".join(f"{name}:{port}" for name, port, _label in pairs)

    env = dict(os.environ)
    env["ADVERTISE"] = advertise

    log_file = open(_DISCOVERY_LOG, "a")
    try:
        proc = subprocess.Popen(
            ["uv", "run", "--with", "minidisc-python", "python", str(agent_script)],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        console.print(f"[red]❌ Failed to start discovery agent: {e}[/red]")
        log_file.close()
        return False

    log_file.close()
    _DISCOVERY_PID.write_text(str(proc.pid))
    console.print(f"[green]✅ Discovery agent started (PID {proc.pid})[/green]")
    return True


def _stop_discovery_agent():
    """Stop the discovery agent process."""
    if not _DISCOVERY_PID.exists():
        _remove_advertised_services()
        return

    try:
        pid = int(_DISCOVERY_PID.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        console.print(f"[green]✅ Discovery agent stopped (PID {pid})[/green]")
    except (ValueError, OSError):
        console.print("[dim]Discovery agent already stopped[/dim]")
    finally:
        _DISCOVERY_PID.unlink(missing_ok=True)
        _remove_advertised_services()


# --- Service manager agent (native process, not Docker) ---
# Host-side HTTP API (edge/service_manager.py) that lets the backend (and thus
# the WebUI System page) start/stop/restart services and switch ASR/TTS
# providers. Runs natively because docker compose needs host bind-mount paths.

_SERVICE_MANAGER_PID = Path(__file__).parent / "edge" / ".service-manager.pid"
_SERVICE_MANAGER_LOG = Path(__file__).parent / "edge" / "service-manager.log"
_SERVICE_MANAGER_PORT = "8775"


def _service_manager_running() -> bool:
    """Check if service manager agent process is alive."""
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
    if not _SERVICE_MANAGER_PID.exists():
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

    # Start discovery agent alongside backend
    if "backend" in services and check_service_enabled("backend"):
        _start_discovery_agent()

    # Start service manager agent (WebUI start/stop control) on any start
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

    # Stop discovery agent when stopping backend
    if "backend" in services:
        _stop_discovery_agent()

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

    # Restart discovery agent alongside backend
    if "backend" in services and check_service_enabled("backend"):
        _stop_discovery_agent()
        _start_discovery_agent()

    # Ensure service manager agent is running
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

    # Discovery agent status
    if _discovery_agent_running():
        pid = int(_DISCOVERY_PID.read_text().strip())
        console.print(f"\n[green]📡 Discovery agent running (PID {pid})[/green]")
    else:
        console.print("\n[dim]📡 Discovery agent not running[/dim]")

    # Service manager agent status
    if _service_manager_running():
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
        "manager_action", choices=["start", "stop", "restart"], help="Agent action"
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


if __name__ == "__main__":
    main()
