#!/usr/bin/env python3
"""
Chronicle ASR Services Setup Script
Interactive configuration for offline ASR (Parakeet) service
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import set_key
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text


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

    def read_existing_env_value(self, key: str) -> str:
        """Read a value from existing .env file"""
        env_path = Path(".env")
        if not env_path.exists():
            return None

        from dotenv import get_key
        value = get_key(str(env_path), key)
        # get_key returns None if key doesn't exist or value is empty
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
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Try to get CUDA version from nvidia-smi
                result = subprocess.run(
                    ["nvidia-smi"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    output = result.stdout
                    # Parse CUDA Version from nvidia-smi output
                    # Format: "CUDA Version: 12.6"
                    import re
                    match = re.search(r'CUDA Version:\s*(\d+)\.(\d+)', output)
                    if match:
                        major, minor = match.groups()
                        cuda_ver = f"{major}.{minor}"

                        # Map to available PyTorch CUDA versions
                        if cuda_ver >= "12.8":
                            return "cu128"
                        elif cuda_ver >= "12.6":
                            return "cu126"
                        elif cuda_ver >= "12.1":
                            return "cu121"
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return "cu126"  # Default fallback to cu126

    def setup_cuda_version(self):
        """Configure PyTorch CUDA version"""
        self.print_section("PyTorch CUDA Version Configuration")

        # Detect macOS (Darwin) and auto-default to CPU
        is_macos = platform.system() == 'Darwin'

        # Check if provided via command line
        if hasattr(self.args, 'pytorch_cuda_version') and self.args.pytorch_cuda_version:
            cuda_version = self.args.pytorch_cuda_version
            self.console.print(f"[green][SUCCESS][/green] PyTorch CUDA version configured from command line: {cuda_version}")
        elif is_macos:
            # Auto-default to CPU on macOS
            cuda_version = "cpu"
            self.console.print("[blue][INFO][/blue] Detected macOS - GPU acceleration not available (Apple Silicon/Intel)")
            self.console.print("[green][SUCCESS][/green] Using CPU-only PyTorch build")
        else:
            # Detect system CUDA version and suggest as default
            detected_cuda = self.detect_cuda_version()

            # Map to default choice number
            cuda_to_choice = {
                "cu121": "1",
                "cu126": "2",
                "cu128": "3"
            }
            default_choice = cuda_to_choice.get(detected_cuda, "2")

            self.console.print()
            self.console.print(f"[blue][INFO][/blue] Detected CUDA version: {detected_cuda}")
            self.console.print("[blue][INFO][/blue] This controls which PyTorch version is installed for GPU acceleration")
            self.console.print()

            cuda_choices = {
                "1": "CUDA 12.1 (cu121)",
                "2": "CUDA 12.6 (cu126) - Recommended",
                "3": "CUDA 12.8 (cu128)"
            }
            cuda_choice = self.prompt_choice(
                "Choose CUDA version for PyTorch:",
                cuda_choices,
                default_choice
            )

            choice_to_cuda = {
                "1": "cu121",
                "2": "cu126",
                "3": "cu128"
            }
            cuda_version = choice_to_cuda[cuda_choice]

        self.config["PYTORCH_CUDA_VERSION"] = cuda_version
        self.console.print(f"[blue][INFO][/blue] Using PyTorch with CUDA version: {cuda_version}")

    def generate_env_file(self):
        """Generate .env file from template and update with configuration"""
        env_path = Path(".env")
        env_template = Path(".env.template")

        # Backup existing .env if it exists
        self.backup_existing_env()

        # Copy template to .env
        if env_template.exists():
            shutil.copy2(env_template, env_path)
            self.console.print("[blue][INFO][/blue] Copied .env.template to .env")
        else:
            self.console.print("[yellow][WARNING][/yellow] .env.template not found, creating new .env")
            env_path.touch(mode=0o600)

        # Update configured values using set_key
        env_path_str = str(env_path)
        for key, value in self.config.items():
            if value:  # Only set non-empty values
                set_key(env_path_str, key, value)

        # Ensure secure permissions
        os.chmod(env_path, 0o600)

        self.console.print("[green][SUCCESS][/green] .env file configured successfully with secure permissions")

    def show_summary(self):
        """Show configuration summary"""
        self.print_section("Configuration Summary")
        self.console.print()

        self.console.print(f"✅ PyTorch CUDA Version: {self.config.get('PYTORCH_CUDA_VERSION', 'Not configured')}")

    def show_next_steps(self):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        self.console.print("1. Start the Parakeet ASR service:")
        self.console.print("   [cyan]docker compose up --build -d parakeet-asr[/cyan]")
        self.console.print()
        self.console.print("2. Service will be available at:")
        self.console.print("   [cyan]http://host.docker.internal:8767[/cyan]")
        self.console.print()
        self.console.print("3. Configure your backend to use offline ASR:")
        self.console.print("   Set PARAKEET_ASR_URL=http://host.docker.internal:8767 in backend .env")

    def run(self):
        """Run the complete setup process"""
        self.print_header("🎤 ASR Services (Parakeet) Setup")
        self.console.print("Configure offline speech-to-text service")
        self.console.print()

        try:
            # Run setup steps
            self.setup_cuda_version()

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()

            # Show results
            self.show_summary()
            self.show_next_steps()

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
    parser = argparse.ArgumentParser(description="ASR Services (Parakeet) Setup")
    parser.add_argument("--pytorch-cuda-version",
                       choices=["cu121", "cu126", "cu128"],
                       help="PyTorch CUDA version (default: auto-detect)")

    args = parser.parse_args()

    setup = ASRServicesSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
