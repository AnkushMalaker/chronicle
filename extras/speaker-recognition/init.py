#!/usr/bin/env python3
"""
Chronicle Speaker Recognition Setup Script
Interactive configuration for speaker recognition service
"""

import argparse
import getpass
import os
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import detect_cuda_version, mask_value, read_env_value


class SpeakerRecognitionSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()

    def print_header(self, title: str):
        """Print a colorful header"""
        self.console.print()
        panel = Panel(Text(title, style="cyan bold"), style="cyan", expand=False)
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

    def prompt_password(self, prompt: str) -> str:
        """Prompt for password (hidden input)"""
        while True:
            try:
                password = getpass.getpass(f"{prompt}: ")
                if password:  # Just need non-empty
                    return password
                self.console.print("[yellow][WARNING][/yellow] Token is required")
            except (EOFError, KeyboardInterrupt):
                self.console.print(
                    "[red][ERROR][/red] Token is required for speaker recognition"
                )
                sys.exit(1)

    def read_existing_env_value(self, key: str) -> str:
        """Read a value from existing .env file (delegates to shared utility)"""
        return read_env_value(".env", key)

    def mask_api_key(self, key: str, show_chars: int = 5) -> str:
        """Mask API key (delegates to shared utility)"""
        return mask_value(key, show_chars)

    def prompt_choice(
        self, prompt: str, choices: Dict[str, str], default: str = "1"
    ) -> str:
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
                self.console.print(
                    f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
                )
            except EOFError:
                self.console.print(f"Using default choice: {default}")
                return default

    def backup_existing_env(self):
        """Backup existing .env file"""
        env_path = Path(".env")
        if env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f".env.backup.{timestamp}"
            shutil.copy2(env_path, backup_path)
            self.console.print(
                f"[blue][INFO][/blue] Backed up existing .env file to {backup_path}"
            )

    def setup_hf_token(self):
        """Configure Hugging Face token"""
        self.print_section("Hugging Face Token Setup")
        self.console.print("Required for pyannote speaker recognition models")
        self.console.print("Get yours from: https://huggingface.co/settings/tokens")
        self.console.print()

        # Check if provided via command line
        if hasattr(self.args, "hf_token") and self.args.hf_token:
            self.config["HF_TOKEN"] = self.args.hf_token
            self.console.print(
                "[green][SUCCESS][/green] HF Token configured from command line"
            )
        else:
            # Check for existing token
            existing_token = self.read_existing_env_value("HF_TOKEN")
            if existing_token and existing_token not in [
                "your_huggingface_token_here",
                "your-hf-token-here",
            ]:
                masked_token = self.mask_api_key(existing_token)
                self.console.print(
                    f"[blue][INFO][/blue] Found existing token: {masked_token}"
                )
                try:
                    reuse = Confirm.ask("Use existing HF Token?", default=True)
                except EOFError:
                    reuse = True

                if reuse:
                    self.config["HF_TOKEN"] = existing_token
                    self.console.print(
                        "[green][SUCCESS][/green] HF Token configured (reused)"
                    )
                else:
                    hf_token = self.prompt_password("HF Token")
                    self.config["HF_TOKEN"] = hf_token
                    self.console.print("[green][SUCCESS][/green] HF Token configured")
            else:
                hf_token = self.prompt_password("HF Token")
                self.config["HF_TOKEN"] = hf_token
                self.console.print("[green][SUCCESS][/green] HF Token configured")

    def detect_cuda_version(self) -> str:
        """Detect system CUDA version (delegates to shared utility)"""
        return detect_cuda_version(default="cu121")

    def setup_compute_mode(self):
        """Configure compute mode (CPU/GPU)"""
        self.print_section("Compute Mode Configuration")

        # Detect macOS (Darwin) and auto-default to CPU
        is_macos = platform.system() == "Darwin"

        # Check if explicit PyTorch version was provided via command line
        if (
            hasattr(self.args, "pytorch_cuda_version")
            and self.args.pytorch_cuda_version
        ):
            self.config["PYTORCH_CUDA_VERSION"] = self.args.pytorch_cuda_version
            compute_mode = (
                "gpu"
                if self.args.pytorch_cuda_version.startswith("cu")
                or self.args.pytorch_cuda_version == "strixhalo"
                else "cpu"
            )
            self.console.print(
                "[green][SUCCESS][/green] PyTorch CUDA version configured from command line: "
                f"{self.args.pytorch_cuda_version}"
            )
        # Check if compute mode was provided via command line
        elif hasattr(self.args, "compute_mode") and self.args.compute_mode:
            compute_mode = self.args.compute_mode
            self.console.print(
                f"[green][SUCCESS][/green] Compute mode configured from command line: {compute_mode}"
            )
        elif is_macos:
            # Auto-default to CPU on macOS
            compute_mode = "cpu"
            self.console.print(
                "[blue][INFO][/blue] Detected macOS - GPU acceleration not available (Apple Silicon/Intel)"
            )
            self.console.print("[green][SUCCESS][/green] Using CPU mode")
        else:
            # Pre-detect NVIDIA GPU to set smart default
            has_nvidia = shutil.which("nvidia-smi") is not None
            default_choice = "2" if has_nvidia else "1"
            if has_nvidia:
                self.console.print(
                    "[blue][INFO][/blue] Detected NVIDIA GPU - defaulting to GPU acceleration"
                )

            choices = {
                "1": "CPU-only (works everywhere)",
                "2": "GPU acceleration (requires NVIDIA+CUDA)",
                "3": "AMD Strix Halo (ROCm, gfx1151 / Ryzen AI Max)",
            }
            choice = self.prompt_choice("Choose compute mode:", choices, default_choice)
            if choice == "1":
                compute_mode = "cpu"
            else:
                compute_mode = "gpu"

        # Set PYTORCH_CUDA_VERSION for Docker build (profile determined from this)
        if "PYTORCH_CUDA_VERSION" in self.config:
            pass
        elif compute_mode == "cpu":
            self.config["PYTORCH_CUDA_VERSION"] = "cpu"
        elif not is_macos and "choice" in locals() and choice == "3":
            self.config["PYTORCH_CUDA_VERSION"] = "strixhalo"
        else:
            # Detect system CUDA version and suggest as default
            detected_cuda = self.detect_cuda_version()

            # Map to default choice number
            cuda_to_choice = {"cu121": "1", "cu126": "2", "cu128": "3"}
            default_choice = cuda_to_choice.get(detected_cuda, "2")

            self.console.print()
            self.console.print(
                f"[blue][INFO][/blue] Detected CUDA version: {detected_cuda}"
            )
            self.console.print()

            cuda_choices = {
                "1": "CUDA 12.1 (cu121)",
                "2": "CUDA 12.6 (cu126)",
                "3": "CUDA 12.8 (cu128)",
            }
            cuda_choice = self.prompt_choice(
                "Choose CUDA version for PyTorch:", cuda_choices, default_choice
            )

            choice_to_cuda = {"1": "cu121", "2": "cu126", "3": "cu128"}
            self.config["PYTORCH_CUDA_VERSION"] = choice_to_cuda[cuda_choice]

        self.console.print(
            f"[blue][INFO][/blue] Using {compute_mode.upper()} mode with PyTorch CUDA version: {self.config['PYTORCH_CUDA_VERSION']}"
        )

        # Set service host and port defaults
        self.config["SPEAKER_SERVICE_HOST"] = "0.0.0.0"
        self.config["SPEAKER_SERVICE_PORT"] = "8085"
        self.config["REACT_UI_HOST"] = "0.0.0.0"

    def setup_deepgram(self):
        """Configure Deepgram API key if provided"""
        # Only set if provided via command line
        if hasattr(self.args, "deepgram_api_key") and self.args.deepgram_api_key:
            self.config["DEEPGRAM_API_KEY"] = self.args.deepgram_api_key
            self.console.print(
                "[green][SUCCESS][/green] Deepgram API key configured from command line"
            )

    def setup_https(self):
        """Configure HTTPS settings using Caddy reverse proxy"""
        # Check if HTTPS configuration provided via command line
        if hasattr(self.args, "enable_https") and self.args.enable_https:
            enable_https = True
            server_ip = getattr(self.args, "server_ip", "localhost")
            self.console.print(
                f"[green][SUCCESS][/green] HTTPS configured via command line: {server_ip}"
            )
        else:
            # Interactive configuration
            self.print_section("HTTPS Configuration (Optional)")
            self.console.print("HTTPS is required for microphone access in browsers")
            self.console.print()

            choices = {
                "1": "HTTP mode (development, localhost only)",
                "2": "HTTPS mode with SSL (production, remote access, microphone access)",
            }
            choice = self.prompt_choice("Choose connection mode:", choices, "1")
            enable_https = choice == "2"

            if enable_https:
                self.console.print()
                self.console.print(
                    "[blue][INFO][/blue] For distributed deployments, use your Tailscale IP"
                )
                self.console.print(
                    "[blue][INFO][/blue] For local-only access, use 'localhost'"
                )
                self.console.print("Examples: localhost, 100.64.1.2, your-domain.com")
                server_ip = self.prompt_value(
                    "Server IP/Domain for SSL certificate", "localhost"
                )

        if enable_https:
            self.config["REACT_UI_HTTPS"] = "true"
            self.config["REACT_UI_PORT"] = "5175"

            # Check for centralized certs (generated by wizard.py)
            certs_dir = Path(__file__).parent / ".." / ".." / "certs"
            cert_file = certs_dir / "server.crt"
            if not cert_file.exists():
                self.console.print(
                    "[yellow][WARNING][/yellow] No certificates found in certs/ directory"
                )
                self.console.print(
                    "[yellow][WARNING][/yellow] Run ./wizard.sh to generate certificates, "
                    "or: cd certs && ./generate-ssl.sh <address>"
                )

            # Generate Caddyfile from template
            self.console.print(
                "[blue][INFO][/blue] Creating Caddyfile configuration..."
            )
            caddyfile_template = Path("Caddyfile.template")
            if caddyfile_template.exists():
                try:
                    with open(caddyfile_template, "r") as f:
                        caddyfile_content = f.read()

                    # Replace placeholders
                    caddyfile_content = caddyfile_content.replace(
                        "TAILSCALE_IP", server_ip
                    )
                    caddyfile_content = caddyfile_content.replace(
                        "WEB_UI_PORT_PLACEHOLDER", self.config["REACT_UI_PORT"]
                    )

                    with open("Caddyfile", "w") as f:
                        f.write(caddyfile_content)

                    self.console.print(
                        f"[green][SUCCESS][/green] Caddyfile created for: {server_ip}"
                    )
                except Exception as e:
                    self.console.print(
                        f"[yellow][WARNING][/yellow] Caddyfile generation failed: {e}"
                    )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] Caddyfile.template not found"
                )

            self.console.print()
            self.console.print("[bold]HTTPS Mode URLs:[/bold]")
            self.console.print(f"   HTTPS Access: https://localhost:8444/")
            self.console.print(f"   HTTP Redirect: http://localhost:8081/ -> HTTPS")
            self.console.print(f"   Service API: https://localhost:8444/api/")
            self.console.print(f"   Accept SSL certificate in browser")
        else:
            self.config["REACT_UI_HTTPS"] = "false"
            self.config["REACT_UI_PORT"] = "5174"

            self.console.print()
            self.console.print("[bold]HTTP Mode URLs:[/bold]")
            self.console.print("   Service API: http://localhost:8085")
            self.console.print("   Web Interface: http://localhost:5174")
            self.console.print(
                "   Note: Microphone access may not work over HTTP on remote connections"
            )

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
            self.console.print(
                "[yellow][WARNING][/yellow] .env.template not found, creating new .env"
            )
            env_path.touch(mode=0o600)

        # Update configured values using set_key
        env_path_str = str(env_path)
        for key, value in self.config.items():
            if value:  # Only set non-empty values
                set_key(env_path_str, key, value)

        # Ensure secure permissions
        os.chmod(env_path, 0o600)

        self.console.print(
            "[green][SUCCESS][/green] .env file configured successfully with secure permissions"
        )

    def show_summary(self):
        """Show configuration summary"""
        self.print_section("Configuration Summary")
        self.console.print()

        self.console.print(
            f"✅ HF Token: {'Configured' if self.config.get('HF_TOKEN') else 'Not configured'}"
        )
        pytorch_version = self.config.get("PYTORCH_CUDA_VERSION", "cpu")
        compute_mode = (
            "GPU"
            if pytorch_version.startswith("cu") or pytorch_version == "strixhalo"
            else "CPU"
        )
        self.console.print(f"✅ Compute Mode: {compute_mode} ({pytorch_version})")
        self.console.print(
            f"✅ HTTPS Enabled: {self.config.get('REACT_UI_HTTPS', 'false')}"
        )
        if self.config.get("DEEPGRAM_API_KEY"):
            self.console.print(f"✅ Deepgram API Key: Configured")

    def show_model_agreement_links(self):
        """Show required Hugging Face model agreement links"""
        self.print_section("⚠️  Required: Accept Model Agreements")
        self.console.print()

        self.console.print(
            "Before using speaker recognition, you must accept agreements for these gated models:"
        )
        self.console.print()

        model_links = [
            (
                "Speaker Diarization",
                "https://huggingface.co/pyannote/speaker-diarization-community-1",
            ),
            ("Segmentation Model", "https://huggingface.co/pyannote/segmentation-3.0"),
            ("Segmentation Model2", "https://huggingface.co/pyannote/segmentation-3.1"),
            (
                "Embedding Model",
                "https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM",
            ),
        ]

        for idx, (name, url) in enumerate(model_links, 1):
            self.console.print(f"   {idx}. [cyan]{name}[/cyan]")
            self.console.print(f"      {url}")
            self.console.print()

        self.console.print(
            "[yellow]→[/yellow] Open each link above and click 'Agree and access repository'"
        )
        self.console.print(
            "[yellow]→[/yellow] You must be logged into Hugging Face with the same account used for HF_TOKEN"
        )
        self.console.print()

    def show_next_steps(self):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        self.console.print("1. Start the speaker recognition service:")
        if self.config.get("REACT_UI_HTTPS") == "true":
            self.console.print("   [cyan]docker compose up --build -d[/cyan]")
        else:
            self.console.print(
                "   [cyan]docker compose up --build -d speaker-service web-ui[/cyan]"
            )

    def run(self):
        """Run the complete setup process"""
        self.print_header("🗣️ Speaker Recognition Setup")
        self.console.print("Configure speaker identification and enrollment service")
        self.console.print()

        try:
            # Run setup steps
            self.setup_hf_token()
            self.setup_compute_mode()
            self.setup_deepgram()
            self.setup_https()

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()

            # Show results
            self.show_summary()
            self.show_model_agreement_links()
            self.show_next_steps()

            self.console.print()
            self.console.print(
                "[green][SUCCESS][/green] Speaker Recognition setup complete! 🎉"
            )

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Speaker Recognition Service Setup")
    parser.add_argument("--hf-token", help="Hugging Face token (default: prompt user)")
    parser.add_argument(
        "--compute-mode",
        choices=["cpu", "gpu"],
        help="Compute mode: cpu or gpu (default: prompt user)",
    )
    parser.add_argument(
        "--pytorch-cuda-version",
        choices=["cpu", "cu121", "cu126", "cu128", "strixhalo"],
        help="PyTorch runtime selection for Docker profile/build",
    )
    parser.add_argument("--deepgram-api-key", help="Deepgram API key (optional)")
    parser.add_argument(
        "--enable-https",
        action="store_true",
        help="Enable HTTPS configuration (default: prompt user)",
    )
    parser.add_argument(
        "--server-ip",
        help="Server IP/domain for SSL certificate (default: prompt user)",
    )

    args = parser.parse_args()

    setup = SpeakerRecognitionSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
