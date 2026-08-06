#!/usr/bin/env python3
"""
Chronicle LLM Services Setup Script
Interactive configuration for llama.cpp-based LLM and embedding services
"""

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from chronicle_setup import ConfigManager, read_env_value
from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

# Anchored to this file, not the working directory: setup runs from the
# repository root so that setup-requirements.txt resolves, but every path a
# service reads or writes belongs to the service's own directory.
SERVICE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_DIR.parent.parent

# LLM model options. The `hf` field is a HuggingFace `repo:quant` reference that
# llama.cpp's server downloads and caches automatically on first start (via
# `--hf-repo` / LLAMA_ARG_HF_REPO), so no local GGUF file is required.
LLM_MODELS = {
    "ggml-org/gemma-4-12B-it-GGUF": {
        "hf": "ggml-org/gemma-4-12B-it-GGUF:Q4_K_M",
        "description": "Gemma 4 12B Instruct Q4_K_M (~7GB, strong general model)",
        "ctx_size": 8192,
    },
    "bartowski/zai-org_GLM-4.7-Flash-GGUF": {
        "hf": "bartowski/zai-org_GLM-4.7-Flash-GGUF:Q4_K_M",
        "description": "GLM-4.7-Flash Q4_K_M (~18GB, 30B MoE / 3B active, fast)",
        "ctx_size": 8192,
    },
    "unsloth/Qwen3.6-27B-GGUF": {
        "hf": "unsloth/Qwen3.6-27B-GGUF:Q4_K_M",
        # Dense 27B, so unlike the GLM MoE above every parameter is read per token
        # — it needs ~16.8GB of weights resident and the whole card to itself.
        "description": "Qwen 3.6 27B Q4_K_M (~17GB, dense, thinking, needs a free 24GB GPU)",
        "ctx_size": 8192,
    },
    "bartowski/Qwen2.5-7B-Instruct-GGUF": {
        "hf": "bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        "description": "Qwen 2.5 7B Instruct Q4_K_M (~4.7GB, multilingual)",
        "ctx_size": 8192,
    },
    "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF": {
        "hf": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M",
        "description": "Llama 3.1 8B Instruct Q4_K_M (~4.9GB, popular)",
        "ctx_size": 8192,
    },
}

DEFAULT_LLM_REPO = "ggml-org/gemma-4-12B-it-GGUF"

# Embedding model options
EMBED_MODELS = {
    "nomic-ai/nomic-embed-text-v1.5-GGUF": {
        "hf": "nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0",
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
        return read_env_value(str(SERVICE_DIR / ".env"), key)

    def resolve_hf_token(self) -> Optional[str]:
        """HF token, in priority order: --hf-token arg, backend .env, repo-root .env,
        this service's own .env.

        Mirrors how the wizard sources shared secrets: ``backends/advanced/.env`` is
        the canonical hub on a main machine; the repo-root ``.env`` is the per-node
        store for backend-less cluster-join nodes. Both are two levels up from here.
        """
        arg_token = getattr(self.args, "hf_token", None)
        if arg_token:
            return arg_token
        for path in (
            str(REPO_ROOT / "backends/advanced/.env"),
            str(REPO_ROOT / ".env"),
            str(SERVICE_DIR / ".env"),
        ):
            value = read_env_value(path, "HF_TOKEN")
            if value:
                return value
        return None

    def backup_existing_env(self):
        env_path = SERVICE_DIR / ".env"
        if env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = SERVICE_DIR / f".env.backup.{timestamp}"
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
        model_choices[custom_key] = (
            "Custom GGUF (HuggingFace repo:quant, or local filename)"
        )

        # Find default choice
        default_choice = "1"
        for i, repo in enumerate(repos, 1):
            if repo == DEFAULT_LLM_REPO:
                default_choice = str(i)
                break

        choice = self.prompt_choice("Choose chat model:", model_choices, default_choice)

        if choice == custom_key:
            self.console.print()
            self.console.print(
                "[blue][INFO][/blue] Enter a HuggingFace GGUF reference such as "
                "[cyan]ggml-org/gemma-4-12B-it-GGUF:Q4_K_M[/cyan] and it will be "
                "downloaded automatically. Or enter a local GGUF filename already "
                "placed in extras/llm-services/models/."
            )
            ref = self.prompt_value(
                "HuggingFace repo:quant reference, or local GGUF filename"
            )
            ctx_size = self.prompt_value("Context size", "8192")

            # A "/" means it's a HuggingFace repo reference (llama.cpp auto-downloads);
            # otherwise treat it as a local filename in the mounted models/ directory.
            ref = ref.strip().removeprefix("https://huggingface.co/")
            if "/" in ref:
                return None, {
                    "hf": ref,
                    "description": f"Custom HuggingFace model ({ref})",
                    "ctx_size": int(ctx_size),
                }
            return None, {
                "file": ref,
                "description": f"Custom local model ({ref})",
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

    def download_models(self, llm_info, embed_info):
        """Report how each selected model will be obtained.

        HuggingFace models (``hf`` set) are downloaded and cached by the llama.cpp
        server itself on first start, so nothing is fetched here. Local models
        (``file`` set) must already be present in extras/llm-services/models/.
        """
        self.print_section("Model Source")

        models_dir = SERVICE_DIR / "models"
        models_dir.mkdir(exist_ok=True)

        for kind, info in (("Chat", llm_info), ("Embedding", embed_info)):
            hf_ref = info.get("hf")
            if hf_ref:
                self.console.print(
                    f"[blue][INFO][/blue] {kind} model [cyan]{hf_ref}[/cyan] will be "
                    "downloaded automatically by llama.cpp on first start "
                    "(cached in extras/llm-services/cache/)."
                )
                continue

            filename = info["file"]
            if (models_dir / filename).exists():
                self.console.print(
                    f"[green]✅[/green] {kind} model present: {filename}"
                )
            else:
                self.console.print(
                    f"[yellow]⚠️  {kind} model file not found: place "
                    f"'{filename}' in extras/llm-services/models/ before starting.[/yellow]"
                )

    def generate_env_file(self):
        """Generate .env file from configuration."""
        env_path = SERVICE_DIR / ".env"
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

        table.add_row("Chat Model", llm_info.get("hf") or llm_info.get("file", ""))
        table.add_row("Chat Port", self.config.get("LLM_PORT", "8081"))
        table.add_row("Context Size", str(self.config.get("CTX_SIZE", "8192")))
        table.add_row(
            "Embedding Model", embed_info.get("hf") or embed_info.get("file", "")
        )
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
            _, llm_info = self.select_llm_model()
            _, embed_info = self.select_embed_model()

            # GPU configuration
            self.setup_gpu_config()

            # Set config values. Either LLM_HF_REPO (auto-download) or
            # LLM_MODEL_FILE (local file) drives the model source; the compose
            # file prefers the HF repo when it is set.
            self.config["LLM_HF_REPO"] = llm_info.get("hf", "")
            self.config["EMBED_HF_REPO"] = embed_info.get("hf", "")
            self.config["LLM_MODEL_FILE"] = llm_info.get("file", "model.gguf")
            self.config["EMBED_MODEL_FILE"] = embed_info.get("file", "embed-model.gguf")
            self.config["CTX_SIZE"] = str(llm_info.get("ctx_size", 8192))
            self.config["EMBED_CTX_SIZE"] = "2048"
            self.config["LLM_PORT"] = "8083"
            self.config["EMBED_PORT"] = "8082"

            # HF token: llama.cpp pulls GGUFs from HuggingFace, so a token avoids IP
            # rate-limiting (429) and unlocks gated repos. Resolve from --hf-token,
            # else keep an existing value, else the repo-root .env.
            hf_token = self.resolve_hf_token()
            if hf_token:
                self.config["HF_TOKEN"] = hf_token

            # Report model sources
            self.download_models(llm_info, embed_info)

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
    parser.add_argument(
        "--hf-token",
        help="Hugging Face token (avoids HF rate-limits / unlocks gated repos)",
    )

    args = parser.parse_args()

    setup = LLMServicesSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
