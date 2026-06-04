#!/usr/bin/env python3
"""
Chronicle TTS Services Setup Script
Interactive configuration for provider-based TTS services
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import detect_cuda_version as _detect_cuda_version
from setup_utils import read_env_value

PROVIDERS = {
    "tada": {
        "name": "TADA",
        "description": "HumeAI TADA - zero-shot voice cloning TTS with 1:1 token alignment (MIT license)",
        "models": {
            "HumeAI/tada-1b": "TADA 1B (English, ~4-5GB VRAM)",
            "HumeAI/tada-3b-ml": "TADA 3B Multilingual (9 languages, ~7-8GB VRAM)",
        },
        "default_model": "HumeAI/tada-1b",
        "service": "tada-tts",
        "capabilities": ["voice_cloning", "speech_continuation"],
    },
    "fish_speech": {
        "name": "Fish Speech",
        "description": "Fish Audio Fish Speech - Dual-AR TTS with emotion control (CC-BY-NC-SA / Research License)",
        "models": {
            "fishaudio/s2-pro": "S2 Pro (83 languages, ~11GB, default)",
            "fishaudio/openaudio-s1-mini": "OpenAudio S1 Mini (0.5B, 50+ languages, ~6GB VRAM, needs tokenizer workarounds)",
            "fishaudio/fish-speech-1.5": "Fish Speech 1.5 (larger, higher quality)",
        },
        "default_model": "fishaudio/s2-pro",
        "service": "fish-tts",
        "capabilities": ["voice_cloning", "multilingual", "emotion_control", "streaming"],
    },
    "kittentts": {
        "name": "KittenTTS",
        "description": "KittenML KittenTTS - ultra-light (~25MB) CPU ONNX TTS, no GPU/API key, English only (Apache 2.0)",
        "models": {
            "KittenML/kitten-tts-mini-0.8": "Mini 0.8 (default)",
            "KittenML/kitten-tts-micro-0.8": "Micro 0.8 (~41MB)",
            "KittenML/kitten-tts-nano-0.8-int8": "Nano 0.8 int8 (~25MB)",
        },
        "default_model": "KittenML/kitten-tts-mini-0.8",
        "service": "kittentts-tts",
        "capabilities": ["lightweight", "cpu", "preset_voices"],
    },
}

# Preset voices for KittenTTS (no zero-shot cloning).
KITTEN_VOICES = ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]

console = Console()
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def detect_cuda_version() -> str:
    return _detect_cuda_version(default="cu126")


def setup_provider() -> Optional[str]:
    """Interactive provider selection."""
    console.print()
    console.print(
        Panel(
            "[bold cyan]Chronicle TTS Services Setup[/bold cyan]",
            border_style="cyan",
        )
    )

    # Show available providers
    table = Table(title="Available TTS Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Capabilities", style="green")

    for key, provider in PROVIDERS.items():
        table.add_row(
            f"{key} ({provider['name']})",
            provider["description"],
            ", ".join(provider["capabilities"]),
        )

    console.print(table)
    console.print()

    # Select provider
    provider_keys = list(PROVIDERS.keys())
    provider_key = Prompt.ask(
        "Select TTS provider",
        choices=provider_keys,
        default=provider_keys[0],
    )

    provider = PROVIDERS[provider_key]
    console.print(f"\n[green]Selected: {provider['name']}[/green]")

    return provider_key


def setup_model(provider_key: str) -> str:
    """Interactive model selection for a provider."""
    provider = PROVIDERS[provider_key]
    models = provider["models"]

    if len(models) == 1:
        model_id = list(models.keys())[0]
        console.print(f"[dim]Using model: {model_id}[/dim]")
        return model_id

    console.print("\n[bold]Available models:[/bold]")
    model_keys = list(models.keys())
    for i, (model_id, description) in enumerate(models.items(), 1):
        default_marker = " [green](default)[/green]" if model_id == provider["default_model"] else ""
        console.print(f"  {i}. {model_id} - {description}{default_marker}")

    console.print(f"  {len(model_keys) + 1}. Custom model (enter HuggingFace repo)")

    choice = Prompt.ask(
        "Select model",
        default="1",
    )

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(model_keys):
            return model_keys[idx]
        elif idx == len(model_keys):
            return Prompt.ask("Enter custom model identifier")
    except ValueError:
        pass

    return provider["default_model"]


def setup_cuda() -> str:
    """Configure CUDA version."""
    detected = detect_cuda_version()
    console.print(f"\n[dim]Detected CUDA version: {detected}[/dim]")

    # hume-tada requires torch>=2.7 which needs CUDA 12.6+
    valid_choices = ["cu126", "cu128"]
    if detected not in valid_choices:
        detected = "cu126"

    cuda_version = Prompt.ask(
        "PyTorch CUDA version (cu121 not supported, torch>=2.7 requires CUDA 12.6+)",
        choices=valid_choices,
        default=detected,
    )

    return cuda_version


def _backup_env() -> None:
    """Back up an existing .env before overwriting."""
    if ENV_FILE.exists():
        backup = ENV_FILE.with_suffix(f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(ENV_FILE, backup)
        console.print(f"[dim]Backed up existing .env to {backup.name}[/dim]")


def write_env(
    provider_key: str,
    model_id: str,
    cuda_version: str,
    port: str = "8770",
    language: str = "",
) -> None:
    """Write .env file for GPU providers (TADA, Fish Speech)."""
    _backup_env()

    # Write values
    ENV_FILE.touch()
    set_key(str(ENV_FILE), "TTS_MODEL", model_id, quote_mode="never")
    set_key(str(ENV_FILE), "TTS_PORT", port, quote_mode="never")
    set_key(str(ENV_FILE), "PYTORCH_CUDA_VERSION", cuda_version, quote_mode="never")
    set_key(str(ENV_FILE), "TTS_LANGUAGE", language, quote_mode="never")

    console.print(f"\n[green]Configuration written to {ENV_FILE}[/green]")


def write_env_kittentts(
    model_id: str,
    voice: str,
    speed: str,
    port: str = "8770",
) -> None:
    """Write .env file for the KittenTTS CPU provider (dedicated KITTEN_TTS_* vars)."""
    _backup_env()

    # KittenTTS uses its own env vars so the heavy Fish/TADA settings don't bleed
    # into this CPU service (matches docker-compose.yml).
    ENV_FILE.touch()
    set_key(str(ENV_FILE), "KITTEN_TTS_MODEL", model_id, quote_mode="never")
    set_key(str(ENV_FILE), "KITTEN_TTS_VOICE", voice, quote_mode="never")
    set_key(str(ENV_FILE), "KITTEN_TTS_SPEED", speed, quote_mode="never")
    set_key(str(ENV_FILE), "KITTEN_TTS_PORT", port, quote_mode="never")

    console.print(f"\n[green]Configuration written to {ENV_FILE}[/green]")


def setup_kittentts(model_id: str) -> None:
    """CPU-only setup flow for KittenTTS (no CUDA, no HF token, preset voices)."""
    voice = Prompt.ask(
        "Preset voice",
        choices=KITTEN_VOICES,
        default="Jasper",
    )
    speed = Prompt.ask("Speech speed multiplier", default="1.0")
    port = Prompt.ask("TTS service port", default="8770")

    write_env_kittentts(model_id, voice, speed, port)

    console.print(
        Panel(
            f"[bold green]Setup Complete![/bold green]\n\n"
            f"Start the service (CPU, no GPU required):\n"
            f"  cd extras/tts\n"
            f"  docker compose up kittentts-tts -d --build\n\n"
            f"Test the service:\n"
            f"  curl http://localhost:{port}/health\n\n"
            f"Synthesize speech:\n"
            f"  curl -X POST http://localhost:{port}/synthesize \\\n"
            f"    -F 'text=Hello, this is a test.' \\\n"
            f"    -o output.wav",
            border_style="green",
        )
    )


def main():
    """Main setup flow."""
    provider_key = setup_provider()
    if provider_key is None:
        return

    model_id = setup_model(provider_key)

    # KittenTTS is CPU-only with a distinct config — handle it separately.
    if provider_key == "kittentts":
        setup_kittentts(model_id)
        return

    cuda_version = setup_cuda()

    # Language config for TADA multilingual model
    language = ""
    if "3b-ml" in model_id:
        language = Prompt.ask(
            "Language code (ar, zh, de, es, fr, it, ja, pl, pt, or empty for English)",
            default="",
        )

    # Fish Speech specific config
    if provider_key == "fish_speech":
        compile_tts = Confirm.ask(
            "Enable torch.compile? (~10x speedup but longer first-inference warmup)",
            default=False,
        )
        ENV_FILE.touch()
        set_key(str(ENV_FILE), "TTS_COMPILE", str(compile_tts).lower(), quote_mode="never")
        set_key(str(ENV_FILE), "TTS_HALF", "true", quote_mode="never")

    port = Prompt.ask("TTS service port", default="8770")

    write_env(provider_key, model_id, cuda_version, port, language)

    # Show next steps
    provider = PROVIDERS[provider_key]
    service_name = provider["service"]

    console.print(
        Panel(
            f"[bold green]Setup Complete![/bold green]\n\n"
            f"Start the service:\n"
            f"  cd extras/tts\n"
            f"  docker compose up {service_name} -d --build\n\n"
            f"Test the service:\n"
            f"  curl http://localhost:{port}/health\n\n"
            f"Synthesize speech:\n"
            f"  curl -X POST http://localhost:{port}/synthesize \\\n"
            f"    -F 'text=Hello, this is a test.' \\\n"
            f"    -o output.wav",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()
