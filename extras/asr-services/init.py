#!/usr/bin/env python3
"""
Chronicle ASR Services Setup Script
Interactive configuration for provider-based ASR services
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import set_key
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text


# Provider and model definitions
PROVIDERS = {
    "vibevoice": {
        "name": "VibeVoice",
        "description": "Microsoft VibeVoice-ASR with built-in speaker diarization",
        "models": {
            "microsoft/VibeVoice-ASR": "VibeVoice-ASR (7B, speaker diarization, 60-min audio)",
        },
        "default_model": "microsoft/VibeVoice-ASR",
        "service": "vibevoice-asr",
        # Note: VibeVoice provides diarization but NOT word_timestamps
        "capabilities": ["segments", "diarization", "timestamps"],
    },
    "faster-whisper": {
        "name": "Faster-Whisper",
        "description": "Fast Whisper inference (4-6x faster) using CTranslate2",
        "models": {
            "Systran/faster-whisper-large-v3": "Whisper Large V3 (Best quality)",
            "Systran/faster-whisper-small": "Whisper Small (Lightweight)",
            "deepdml/faster-whisper-large-v3-turbo-ct2": "Whisper Large V3 Turbo (Speed optimized)",
        },
        "default_model": "Systran/faster-whisper-large-v3",
        "service": "faster-whisper-asr",
        "capabilities": ["timestamps", "word_timestamps", "language_detection", "vad_filter", "translation"],
    },
    "transformers": {
        "name": "Transformers",
        "description": "HuggingFace models (Hindi Whisper, custom models)",
        "models": {
            "Oriserve/Whisper-Hindi2Hinglish-Prime": "Hindi/Hinglish Whisper (Fine-tuned Large V3)",
            "openai/whisper-large-v3": "OpenAI Whisper Large V3",
        },
        "default_model": "openai/whisper-large-v3",
        "service": "transformers-asr",
        "capabilities": ["timestamps", "word_timestamps", "language_detection"],
    },
    "nemo": {
        "name": "NeMo",
        "description": "NVIDIA NeMo ASR models (Parakeet, Canary)",
        "models": {
            "nvidia/parakeet-tdt-0.6b-v3": "Parakeet TDT 0.6B v3 (Default)",
            "nvidia/canary-1b": "Canary 1B (Multilingual)",
        },
        "default_model": "nvidia/parakeet-tdt-0.6b-v3",
        "service": "nemo-asr",
        "capabilities": ["timestamps", "word_timestamps", "chunked_processing"],
    },
}


class ASRServicesSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()

    def print_header(self, title: str):
        """Print a colorful header"""
        self.console.print()
        panel = Panel(
            Text(title, style="cyan bold"),
            style="cyan",
            expand=False
        )
        self.console.print(panel)
        self.console.print()

    def print_section(self, title: str):
        """Print a section header"""
        self.console.print()
        self.console.print(f"[magenta]► {title}[/magenta]")
        self.console.print("[magenta]" + "─" * len(f"► {title}") + "[/magenta]")

    def prompt_value(self, prompt: str, default: str = "") -> str:
        """Prompt for a value with optional default"""
        try:
            return Prompt.ask(prompt, default=default)
        except EOFError:
            self.console.print(f"Using default: {default}")
            return default

    def prompt_choice(self, prompt: str, choices: Dict[str, str], default: str = "1") -> str:
        """Prompt for a choice from options"""
        self.console.print(prompt)
        for key, desc in choices.items():
            self.console.print(f"  {key}) {desc}")
        self.console.print()

        while True:
            try:
                choice = Prompt.ask("Enter choice", default=default)
                if choice in choices:
                    return choice
                self.console.print(f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]")
            except EOFError:
                self.console.print(f"Using default choice: {default}")
                return default

    def read_existing_env_value(self, key: str) -> Optional[str]:
        """Read a value from existing .env file"""
        env_path = Path(".env")
        if not env_path.exists():
            return None

        from dotenv import get_key
        value = get_key(str(env_path), key)
        return value if value else None

    def backup_existing_env(self):
        """Backup existing .env file"""
        env_path = Path(".env")
        if env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f".env.backup.{timestamp}"
            shutil.copy2(env_path, backup_path)
            self.console.print(f"[blue][INFO][/blue] Backed up existing .env file to {backup_path}")

    def detect_cuda_version(self) -> str:
        """Detect system CUDA version from nvidia-smi"""
        try:
            result = subprocess.run(
                ["nvidia-smi"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                output = result.stdout
                match = re.search(r'CUDA Version:\s*(\d+)\.(\d+)', output)
                if match:
                    major, minor = match.groups()
                    cuda_ver = f"{major}.{minor}"
                    if cuda_ver >= "12.8":
                        return "cu128"
                    elif cuda_ver >= "12.6":
                        return "cu126"
                    elif cuda_ver >= "12.1":
                        return "cu121"
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return "cu126"

    def select_provider(self) -> str:
        """Select ASR provider"""
        self.print_section("Provider Selection")

        # Show provider comparison table
        table = Table(title="Available ASR Providers")
        table.add_column("Provider", style="cyan")
        table.add_column("Description", style="white")
        table.add_column("Best For", style="green")

        table.add_row(
            "vibevoice",
            "Microsoft VibeVoice-ASR",
            "Built-in speaker diarization"
        )
        table.add_row(
            "faster-whisper",
            "Fast Whisper (CTranslate2)",
            "General use, speed + quality"
        )
        table.add_row(
            "transformers",
            "HuggingFace models",
            "Hindi, custom models"
        )
        table.add_row(
            "nemo",
            "NVIDIA NeMo",
            "Parakeet, long audio processing"
        )
        self.console.print(table)
        self.console.print()

        # Check for command-line provider
        if hasattr(self.args, 'provider') and self.args.provider:
            provider = self.args.provider
            self.console.print(f"[green][SUCCESS][/green] Provider set from command line: {provider}")
            return provider

        provider_choices = {
            "1": "vibevoice - Microsoft VibeVoice-ASR (Built-in diarization)",
            "2": "faster-whisper - Fast Whisper inference (Recommended for general use)",
            "3": "transformers - HuggingFace models (Hindi, custom)",
            "4": "nemo - NVIDIA NeMo (Parakeet)",
        }

        choice = self.prompt_choice("Choose ASR provider:", provider_choices, "2")
        choice_to_provider = {"1": "vibevoice", "2": "faster-whisper", "3": "transformers", "4": "nemo"}
        return choice_to_provider[choice]

    def select_model(self, provider: str) -> str:
        """Select model for the chosen provider"""
        self.print_section(f"Model Selection ({PROVIDERS[provider]['name']})")

        provider_info = PROVIDERS[provider]
        models = provider_info["models"]
        default_model = provider_info["default_model"]

        # Check for command-line model
        if hasattr(self.args, 'model') and self.args.model:
            model = self.args.model
            self.console.print(f"[green][SUCCESS][/green] Model set from command line: {model}")
            return model

        # Show available models
        self.console.print(f"[blue]Available models for {provider_info['name']}:[/blue]")
        model_choices = {}
        for i, (model_id, description) in enumerate(models.items(), 1):
            model_choices[str(i)] = f"{model_id} - {description}"
            if model_id == default_model:
                model_choices[str(i)] += " (Default)"

        # Find default choice number
        default_choice = "1"
        for i, model_id in enumerate(models.keys(), 1):
            if model_id == default_model:
                default_choice = str(i)
                break

        # Add custom model option
        model_choices[str(len(models) + 1)] = "Enter custom model URL"

        choice = self.prompt_choice("Choose model:", model_choices, default_choice)

        if choice == str(len(models) + 1):
            # Custom model
            custom_model = self.prompt_value("Enter model identifier (HuggingFace repo or path)")
            return custom_model
        else:
            return list(models.keys())[int(choice) - 1]

    def setup_cuda_version(self):
        """Configure PyTorch CUDA version"""
        self.print_section("PyTorch CUDA Version")

        is_macos = platform.system() == 'Darwin'

        if hasattr(self.args, 'pytorch_cuda_version') and self.args.pytorch_cuda_version:
            cuda_version = self.args.pytorch_cuda_version
            self.console.print(f"[green][SUCCESS][/green] CUDA version from command line: {cuda_version}")
        elif is_macos:
            cuda_version = "cpu"
            self.console.print("[blue][INFO][/blue] Detected macOS - using CPU-only PyTorch")
        else:
            detected_cuda = self.detect_cuda_version()
            self.console.print(f"[blue][INFO][/blue] Detected CUDA version: {detected_cuda}")

            cuda_choices = {
                "1": "CUDA 12.1 (cu121)",
                "2": "CUDA 12.6 (cu126) - Recommended",
                "3": "CUDA 12.8 (cu128)",
            }
            cuda_to_choice = {"cu121": "1", "cu126": "2", "cu128": "3"}
            default_choice = cuda_to_choice.get(detected_cuda, "2")

            choice = self.prompt_choice("Choose CUDA version:", cuda_choices, default_choice)
            choice_to_cuda = {"1": "cu121", "2": "cu126", "3": "cu128"}
            cuda_version = choice_to_cuda[choice]

        self.config["PYTORCH_CUDA_VERSION"] = cuda_version

    def setup_provider_config(self, provider: str, model: str):
        """Configure provider-specific settings"""
        self.print_section("Provider Configuration")

        self.config["ASR_PROVIDER"] = provider
        self.config["ASR_MODEL"] = model
        self.config["ASR_PORT"] = "8767"

        if provider == "faster-whisper":
            self.config["COMPUTE_TYPE"] = "float16"
            self.config["DEVICE"] = "cuda"
            self.config["VAD_FILTER"] = "true"

            # Ask about language
            if Confirm.ask("Force specific language?", default=False):
                lang = self.prompt_value("Language code (e.g., en, hi, es)", default="")
                if lang:
                    self.config["LANGUAGE"] = lang

        elif provider == "vibevoice":
            # VibeVoice uses transformers backend with specific optimizations
            self.config["TORCH_DTYPE"] = "float16"
            self.config["DEVICE"] = "cuda"
            self.config["USE_FLASH_ATTENTION"] = "true"
            self.console.print("[blue][INFO][/blue] Enabled Flash Attention for VibeVoice")
            self.console.print("[blue][INFO][/blue] VibeVoice provides built-in speaker diarization (no pyannote needed)")

        elif provider == "transformers":
            self.config["TORCH_DTYPE"] = "float16"
            self.config["DEVICE"] = "cuda"
            self.config["USE_FLASH_ATTENTION"] = "false"

            # Hindi model-specific
            if "hindi" in model.lower():
                self.config["LANGUAGE"] = "hi"
                self.console.print("[blue][INFO][/blue] Set language to Hindi for Hindi Whisper model")

        elif provider == "nemo":
            # NeMo's transcribe() handles long audio natively - no extra config needed
            pass

    def generate_env_file(self):
        """Generate .env file from configuration"""
        env_path = Path(".env")
        env_template = Path(".env.template")

        self.backup_existing_env()

        if env_template.exists():
            shutil.copy2(env_template, env_path)
            self.console.print("[blue][INFO][/blue] Copied .env.template to .env")
        else:
            env_path.touch(mode=0o600)

        env_path_str = str(env_path)
        for key, value in self.config.items():
            if value:
                set_key(env_path_str, key, value)

        os.chmod(env_path, 0o600)
        self.console.print("[green][SUCCESS][/green] .env file configured successfully")

    def show_summary(self, provider: str, model: str):
        """Show configuration summary"""
        self.print_section("Configuration Summary")
        self.console.print()

        provider_info = PROVIDERS[provider]

        table = Table(title="ASR Service Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Provider", f"{provider_info['name']} ({provider})")
        table.add_row("Model", model)
        table.add_row("Port", self.config.get("ASR_PORT", "8767"))
        table.add_row("CUDA Version", self.config.get("PYTORCH_CUDA_VERSION", "N/A"))
        table.add_row("Capabilities", ", ".join(provider_info["capabilities"]))

        self.console.print(table)

    def show_next_steps(self, provider: str):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        service_name = PROVIDERS[provider]["service"]

        self.console.print("1. Build and start the ASR service:")
        self.console.print(f"   [cyan]docker compose up --build -d {service_name}[/cyan]")
        self.console.print()
        self.console.print("2. Or use a pre-configured profile:")
        self.console.print("   [cyan]cp configs/parakeet.env .env && docker compose up --build -d nemo-asr[/cyan]")
        self.console.print()
        self.console.print("3. Service will be available at:")
        self.console.print(f"   [cyan]http://localhost:{self.config.get('ASR_PORT', '8767')}[/cyan]")
        self.console.print()
        self.console.print("4. Test the service:")
        self.console.print(f"   [cyan]curl http://localhost:{self.config.get('ASR_PORT', '8767')}/health[/cyan]")
        self.console.print()
        self.console.print("5. Configure Chronicle backend:")
        self.console.print(f"   Set PARAKEET_ASR_URL=http://host.docker.internal:{self.config.get('ASR_PORT', '8767')}")

    def run(self):
        """Run the complete setup process"""
        self.print_header("🎤 ASR Services Setup (Provider-Based Architecture)")
        self.console.print("Configure offline speech-to-text service with your choice of provider and model")
        self.console.print()

        try:
            # Select provider and model
            provider = self.select_provider()
            model = self.select_model(provider)

            # Configure CUDA version (only for providers that need it)
            if provider in ["nemo", "transformers"]:
                self.setup_cuda_version()

            # Provider-specific configuration
            self.setup_provider_config(provider, model)

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()

            # Show results
            self.show_summary(provider, model)
            self.show_next_steps(provider)

            self.console.print()
            self.console.print("[green][SUCCESS][/green] ASR Services setup complete! 🎉")

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="ASR Services Setup (Provider-Based)")
    parser.add_argument(
        "--provider",
        choices=["vibevoice", "faster-whisper", "transformers", "nemo"],
        help="ASR provider to use"
    )
    parser.add_argument(
        "--model",
        help="Model identifier (HuggingFace repo or path)"
    )
    parser.add_argument(
        "--pytorch-cuda-version",
        choices=["cu121", "cu126", "cu128"],
        help="PyTorch CUDA version"
    )

    args = parser.parse_args()

    setup = ASRServicesSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
