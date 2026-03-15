#!/usr/bin/env python3
"""
Chronicle LLM Services Setup Script
Interactive configuration for llama.cpp-based LLM and embedding services
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config_manager import ConfigManager
from setup_utils import read_env_value

# LLM model options (HuggingFace GGUF repos)
LLM_MODELS = {
    "bartowski/zai-org_GLM-4.7-Flash-GGUF": {
        "file": "zai-org_GLM-4.7-Flash-Q4_K_M.gguf",
        "description": "GLM-4.7-Flash Q4_K_M (~18GB, 30B MoE / 3B active, fast)",
        "ctx_size": 8192,
    },
    "bartowski/Qwen2.5-7B-Instruct-GGUF": {
        "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "description": "Qwen 2.5 7B Instruct Q4_K_M (~4.7GB, multilingual)",
        "ctx_size": 8192,
    },
    "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF": {
        "file": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "description": "Llama 3.1 8B Instruct Q4_K_M (~4.9GB, popular)",
        "ctx_size": 8192,
    },
}

DEFAULT_LLM_REPO = "bartowski/zai-org_GLM-4.7-Flash-GGUF"

# Embedding model options
EMBED_MODELS = {
    "nomic-ai/nomic-embed-text-v1.5-GGUF": {
        "file": "nomic-embed-text-v1.5.Q8_0.gguf",
        "description": "Nomic Embed Text v1.5 Q8 (~140MB, 768 dims)",
        "dimensions": 768,
    },
}

DEFAULT_EMBED_REPO = "nomic-ai/nomic-embed-text-v1.5-GGUF"


class LLMServicesSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()

    def print_header(self, title: str):
        self.console.print()
        panel = Panel(Text(title, style="cyan bold"), style="cyan", expand=False)
        self.console.print(panel)
        self.console.print()

    def print_section(self, title: str):
        self.console.print()
        self.console.print(f"[magenta]► {title}[/magenta]")
        self.console.print("[magenta]" + "─" * len(f"► {title}") + "[/magenta]")

    def prompt_value(self, prompt: str, default: str = "") -> str:
        try:
            return Prompt.ask(prompt, default=default)
        except EOFError:
            self.console.print(f"Using default: {default}")
            return default

    def prompt_choice(
        self, prompt: str, choices: Dict[str, str], default: str = "1"
    ) -> str:
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

    def read_existing_env_value(self, key: str) -> Optional[str]:
        return read_env_value(".env", key)

    def backup_existing_env(self):
        env_path = Path(".env")
        if env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f".env.backup.{timestamp}"
            shutil.copy2(env_path, backup_path)
            self.console.print(
                f"[blue][INFO][/blue] Backed up existing .env file to {backup_path}"
            )

    def select_llm_model(self) -> tuple:
        """Select LLM chat model. Returns (repo, model_info)."""
        self.print_section("Chat Model Selection")

        model_choices = {}
        repos = list(LLM_MODELS.keys())
        for i, (repo, info) in enumerate(LLM_MODELS.items(), 1):
            default_marker = " (Default)" if repo == DEFAULT_LLM_REPO else ""
            model_choices[str(i)] = f"{info['description']}{default_marker}"

        custom_key = str(len(repos) + 1)
        model_choices[custom_key] = "Custom GGUF (enter filename)"

        # Find default choice
        default_choice = "1"
        for i, repo in enumerate(repos, 1):
            if repo == DEFAULT_LLM_REPO:
                default_choice = str(i)
                break

        choice = self.prompt_choice("Choose chat model:", model_choices, default_choice)

        if choice == custom_key:
            filename = self.prompt_value(
                "Enter GGUF filename (must be in extras/llm-services/models/)"
            )
            ctx_size = self.prompt_value("Context size", "8192")
            return None, {
                "file": filename,
                "description": f"Custom model ({filename})",
                "ctx_size": int(ctx_size),
            }
        else:
            repo = repos[int(choice) - 1]
            return repo, LLM_MODELS[repo]

    def select_embed_model(self) -> tuple:
        """Select embedding model. Returns (repo, model_info)."""
        self.print_section("Embedding Model Selection")

        repos = list(EMBED_MODELS.keys())
        repo = repos[0]
        info = EMBED_MODELS[repo]
        self.console.print(f"[green]✅[/green] Using: {info['description']}")
        return repo, info

    def setup_gpu_config(self):
        """Configure GPU layers and context size."""
        self.print_section("GPU Configuration")

        self.console.print(
            "[blue][INFO][/blue] llama.cpp uses pre-built CUDA Docker images (no local CUDA build)"
        )

        n_gpu_layers = self.prompt_value(
            "GPU layers (-1 = all layers on GPU, 0 = CPU only)", "-1"
        )
        self.config["N_GPU_LAYERS"] = n_gpu_layers

    def download_models(self, llm_repo, llm_info, embed_repo, embed_info):
        """Offer to download model files."""
        self.print_section("Model Download")

        models_dir = Path("models")
        models_dir.mkdir(exist_ok=True)

        llm_file = models_dir / llm_info["file"]
        embed_file = models_dir / embed_info["file"]

        missing = []
        if not llm_file.exists():
            missing.append(("chat", llm_repo, llm_info["file"]))
        else:
            self.console.print(
                f"[green]✅[/green] Chat model exists: {llm_info['file']}"
            )

        if not embed_file.exists():
            missing.append(("embedding", embed_repo, embed_info["file"]))
        else:
            self.console.print(
                f"[green]✅[/green] Embedding model exists: {embed_info['file']}"
            )

        if not missing:
            self.console.print("[green]✅[/green] All model files present")
            return

        self.console.print()
        for kind, repo, filename in missing:
            self.console.print(f"[yellow]⚠️  Missing {kind} model: {filename}[/yellow]")

        try:
            download_now = Confirm.ask(
                "Download missing models now? (may take a while for large files)",
                default=True,
            )
        except EOFError:
            download_now = False

        if download_now:
            for kind, repo, filename in missing:
                if repo is None:
                    self.console.print(
                        f"[yellow]⚠️  Custom model '{filename}' - place it in extras/llm-services/models/ manually[/yellow]"
                    )
                    continue

                self.console.print(
                    f"[cyan]📥 Downloading {kind} model: {filename}...[/cyan]"
                )
                try:
                    dest = str(models_dir / filename)
                    if shutil.which("huggingface-cli"):
                        subprocess.run(
                            [
                                "huggingface-cli",
                                "download",
                                repo,
                                filename,
                                "--local-dir",
                                str(models_dir),
                                "--local-dir-use-symlinks",
                                "False",
                            ],
                            check=True,
                        )
                    elif shutil.which("wget"):
                        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
                        subprocess.run(["wget", "-O", dest, url], check=True)
                    elif shutil.which("curl"):
                        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
                        subprocess.run(["curl", "-L", "-o", dest, url], check=True)
                    else:
                        self.console.print(
                            "[yellow]⚠️  No download tool found. Install huggingface-cli, wget, or curl.[/yellow]"
                        )
                        self.console.print(
                            f"[blue][INFO][/blue] Run: ./download-models.sh"
                        )
                        continue

                    self.console.print(f"[green]✅[/green] Downloaded {filename}")
                except subprocess.CalledProcessError:
                    self.console.print(f"[red]❌[/red] Failed to download {filename}")
                    self.console.print(
                        f"[blue][INFO][/blue] Run manually: ./download-models.sh"
                    )
        else:
            self.console.print()
            self.console.print(
                "[blue][INFO][/blue] Download models later with: ./download-models.sh"
            )

    def generate_env_file(self):
        """Generate .env file from configuration."""
        env_path = Path(".env")
        self.backup_existing_env()
        env_path.touch(mode=0o600)

        env_path_str = str(env_path)
        for key, value in self.config.items():
            if value is not None:
                set_key(env_path_str, key, str(value))

        os.chmod(env_path, 0o600)
        self.console.print("[green][SUCCESS][/green] .env file configured successfully")

    def update_config_yml(self):
        """Update config/config.yml with llama.cpp model defaults."""
        try:
            config_manager = ConfigManager(service_path="extras/llm-services")
            config = config_manager.get_full_config()
            models = config.get("models", []) or []
            model_names = [m.get("name") for m in models]

            needed_models = ["llamacpp-llm", "llamacpp-embed"]
            missing = [name for name in needed_models if name not in model_names]

            if missing:
                # Load defaults.yml to get model definitions
                defaults_path = config_manager.config_dir / "defaults.yml"
                if defaults_path.exists():
                    import yaml

                    with open(defaults_path) as f:
                        defaults = yaml.safe_load(f) or {}
                    defaults_models = defaults.get("models", []) or []
                    defaults_by_name = {
                        m["name"]: m for m in defaults_models if "name" in m
                    }

                    for name in missing:
                        if name in defaults_by_name:
                            models.append(defaults_by_name[name])
                            self.console.print(
                                f"[green][SUCCESS][/green] Added model '{name}' to config.yml from defaults"
                            )
                        else:
                            self.console.print(
                                f"[yellow][WARNING][/yellow] Model '{name}' not found in defaults.yml"
                            )

                    config["models"] = models
                    config_manager.save_full_config(config)
            else:
                self.console.print(
                    "[blue][INFO][/blue] Model definitions already present in config.yml"
                )

            # Update defaults
            config_manager.update_config_defaults(
                {"llm": "llamacpp-llm", "embedding": "llamacpp-embed"}
            )

            self.console.print(
                "[green][SUCCESS][/green] Updated defaults.llm to 'llamacpp-llm' in config/config.yml"
            )
            self.console.print(
                "[green][SUCCESS][/green] Updated defaults.embedding to 'llamacpp-embed' in config/config.yml"
            )

        except Exception as e:
            self.console.print(
                f"[yellow][WARNING][/yellow] Could not update config.yml: {e}"
            )
            self.console.print(
                "[blue][INFO][/blue] You may need to manually set defaults in config/config.yml"
            )

    def show_summary(self, llm_info, embed_info):
        """Show configuration summary."""
        self.print_section("Configuration Summary")
        self.console.print()

        table = Table(title="LLM Service Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Chat Model", llm_info["file"])
        table.add_row("Chat Port", self.config.get("LLM_PORT", "8081"))
        table.add_row("Context Size", str(self.config.get("CTX_SIZE", "8192")))
        table.add_row("Embedding Model", embed_info["file"])
        table.add_row("Embedding Port", self.config.get("EMBED_PORT", "8082"))
        table.add_row("Embedding Dimensions", str(embed_info.get("dimensions", "768")))
        table.add_row("GPU Layers", self.config.get("N_GPU_LAYERS", "-1"))

        self.console.print(table)

    def show_next_steps(self):
        """Show next steps."""
        self.print_section("Next Steps")
        self.console.print()

        self.console.print("1. Start the LLM services:")
        self.console.print("   [cyan]docker compose up -d[/cyan]")
        self.console.print()
        self.console.print("2. Services will be available at:")
        llm_port = self.config.get("LLM_PORT", "8081")
        embed_port = self.config.get("EMBED_PORT", "8082")
        self.console.print(f"   Chat:      [cyan]http://localhost:{llm_port}[/cyan]")
        self.console.print(f"   Embedding: [cyan]http://localhost:{embed_port}[/cyan]")
        self.console.print()
        self.console.print("3. Test health:")
        self.console.print(f"   [cyan]curl http://localhost:{llm_port}/health[/cyan]")
        self.console.print(f"   [cyan]curl http://localhost:{embed_port}/health[/cyan]")

    def run(self):
        """Run the complete setup process."""
        self.print_header("🧠 LLM Services Setup (llama.cpp)")
        self.console.print("Configure local LLM and embedding services via llama.cpp")
        self.console.print()

        try:
            # Select models
            llm_repo, llm_info = self.select_llm_model()
            embed_repo, embed_info = self.select_embed_model()

            # GPU configuration
            self.setup_gpu_config()

            # Set config values
            self.config["LLM_MODEL_FILE"] = llm_info["file"]
            self.config["EMBED_MODEL_FILE"] = embed_info["file"]
            self.config["CTX_SIZE"] = str(llm_info.get("ctx_size", 8192))
            self.config["EMBED_CTX_SIZE"] = "2048"
            self.config["LLM_PORT"] = "8083"
            self.config["EMBED_PORT"] = "8082"

            # Download models
            self.download_models(llm_repo, llm_info, embed_repo, embed_info)

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()

            # Update config/config.yml
            self.update_config_yml()

            # Show results
            self.show_summary(llm_info, embed_info)
            self.show_next_steps()

            self.console.print()
            self.console.print(
                "[green][SUCCESS][/green] LLM Services setup complete! 🎉"
            )

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="LLM Services Setup (llama.cpp)")
    parser.add_argument(
        "--llm-model",
        help="LLM model GGUF filename",
    )
    parser.add_argument(
        "--embed-model",
        help="Embedding model GGUF filename",
    )
    parser.add_argument(
        "--n-gpu-layers",
        default="-1",
        help="Number of GPU layers (-1 = all)",
    )
    parser.add_argument(
        "--ctx-size",
        default="8192",
        help="Context size for chat model",
    )

    args = parser.parse_args()

    setup = LLMServicesSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
