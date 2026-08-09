#!/usr/bin/env python3
"""
Chronicle LLM Services Setup Script
Interactive configuration for llama.cpp-based LLM and embedding services
"""

import argparse
import ipaddress
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

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
        "thinking": True,
    },
    "bartowski/zai-org_GLM-4.7-Flash-GGUF": {
        "hf": "bartowski/zai-org_GLM-4.7-Flash-GGUF:Q4_K_M",
        "description": "GLM-4.7-Flash Q4_K_M (~18GB, 30B MoE / 3B active, fast)",
        "ctx_size": 8192,
        "thinking": True,
    },
    "unsloth/Qwen3.6-27B-GGUF": {
        "hf": "unsloth/Qwen3.6-27B-GGUF:Q4_K_M",
        # Dense 27B, so unlike the GLM MoE above every parameter is read per token
        # — it needs ~16.8GB of weights resident and the whole card to itself.
        "description": "Qwen 3.6 27B Q4_K_M (~17GB, dense, thinking, needs a free 24GB GPU)",
        # Validated on a 24GB A30: Q8 KV at 64K uses 18,344 MiB with the
        # text-only/no-projector profile and has effectively the same 7.5K-token
        # prompt-processing speed as 32K.
        "ctx_size": 65536,
        "thinking": True,
    },
    "bartowski/Qwen2.5-7B-Instruct-GGUF": {
        "hf": "bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        "description": "Qwen 2.5 7B Instruct Q4_K_M (~4.7GB, multilingual)",
        "ctx_size": 8192,
        "thinking": False,
    },
    "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF": {
        "hf": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M",
        "description": "Llama 3.1 8B Instruct Q4_K_M (~4.9GB, popular)",
        "ctx_size": 8192,
        "thinking": False,
    },
}

DEFAULT_LLM_REPO = "ggml-org/gemma-4-12B-it-GGUF"
QWEN36_REPO = "unsloth/Qwen3.6-27B-GGUF"
MANAGED_LLAMACPP_REGISTRY_MODELS = {"llamacpp-llm", "qwen36-llm"}
DEFAULT_CUSTOM_CONTEXT_WINDOW = 8192
DEFAULT_PI_MAX_TOKENS = 4096
PI_PROMPT_HEADROOM_TOKENS = 1024
TAILSCALE_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# llama.cpp's documented defaults for the knobs Chronicle changes in the Qwen
# text-agent profile. Keeping these explicit prevents a rerun from leaving stale
# Qwen-only values behind when another model is selected.
DEFAULT_SERVER_PROFILE = {
    "LLAMA_ARG_N_PARALLEL": "1",
    "LLAMA_ARG_FLASH_ATTN": "auto",
    "LLAMA_ARG_JINJA": "true",
    "LLAMA_ARG_CACHE_TYPE_K": "f16",
    "LLAMA_ARG_CACHE_TYPE_V": "f16",
    "LLAMA_ARG_MMPROJ_AUTO": "true",
    "LLAMA_ARG_THINK_BUDGET": "-1",
}
QWEN_TEXT_SERVER_PROFILE = {
    **DEFAULT_SERVER_PROFILE,
    "LLAMA_ARG_N_PARALLEL": "1",
    "LLAMA_ARG_FLASH_ATTN": "on",
    "LLAMA_ARG_CACHE_TYPE_K": "q8_0",
    "LLAMA_ARG_CACHE_TYPE_V": "q8_0",
    # `-hf` otherwise auto-downloads and loads Qwen's ~0.93GB vision projector.
    # Chronicle's local memory service is text-only.
    "LLAMA_ARG_MMPROJ_AUTO": "false",
    # Thinking is unrestricted by default, and a memory prompt already reaches ~15K
    # of the served window. Cap the trace so an unbounded one cannot crowd out the
    # transcript and tool results the agent still has to read.
    "LLAMA_ARG_THINK_BUDGET": "2000",
}

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

    @staticmethod
    def _positive_context_window(value: Any) -> int:
        try:
            context_window = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Context size must be a positive integer") from exc
        if context_window <= 0:
            raise ValueError("Context size must be a positive integer")
        return context_window

    def _context_window(self, default: int) -> int:
        override = getattr(self.args, "ctx_size", None)
        return self._positive_context_window(
            default if override in (None, "") else override
        )

    @staticmethod
    def _known_repo(reference: str, models: Dict[str, Dict[str, Any]]) -> Optional[str]:
        normalized = reference.strip().removeprefix("https://huggingface.co/")
        for repo, info in models.items():
            hf_ref = str(info.get("hf") or "")
            if normalized in {repo, hf_ref, hf_ref.split(":", 1)[0]}:
                return repo
        return None

    def _llm_from_reference(self, reference: str) -> tuple:
        normalized = reference.strip().removeprefix("https://huggingface.co/")
        if not normalized:
            raise ValueError("LLM model reference must not be empty")
        known_repo = self._known_repo(normalized, LLM_MODELS)
        if known_repo:
            info = dict(LLM_MODELS[known_repo])
            info["ctx_size"] = self._context_window(int(info["ctx_size"]))
            return known_repo, info

        context_window = self._context_window(DEFAULT_CUSTOM_CONTEXT_WINDOW)
        if "/" in normalized:
            return None, {
                "hf": normalized,
                "description": f"Custom HuggingFace model ({normalized})",
                "ctx_size": context_window,
                "thinking": False,
            }
        return None, {
            "file": normalized,
            "description": f"Custom local model ({normalized})",
            "ctx_size": context_window,
            "thinking": False,
        }

    def _embed_from_reference(self, reference: str) -> tuple:
        normalized = reference.strip().removeprefix("https://huggingface.co/")
        if not normalized:
            raise ValueError("Embedding model reference must not be empty")
        known_repo = self._known_repo(normalized, EMBED_MODELS)
        if known_repo:
            return known_repo, dict(EMBED_MODELS[known_repo])
        if "/" in normalized:
            return None, {
                "hf": normalized,
                "description": f"Custom HuggingFace embedding model ({normalized})",
                "dimensions": 768,
            }
        return None, {
            "file": normalized,
            "description": f"Custom local embedding model ({normalized})",
            "dimensions": 768,
        }

    def _current_llm_repo(self) -> Optional[str]:
        """Resolve the previously served model so reruns default to it."""
        for key in ("LLM_HF_REPO", "LLM_MODEL_FILE"):
            reference = self.read_existing_env_value(key)
            if reference:
                known_repo = self._known_repo(reference, LLM_MODELS)
                if known_repo:
                    return known_repo
        return None

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

    def configure_network_contract(self) -> None:
        """Default host publication to loopback; require auth for wider binds."""
        api_key = self.read_existing_env_value("LLAMA_API_KEY") or ""

        def validated_host(key: str, label: str) -> tuple[str, ipaddress.IPv4Address]:
            host = (self.read_existing_env_value(key) or "127.0.0.1").strip()
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError(
                    f"{label} bind host must be an IPv4 address (use 127.0.0.1 locally)"
                ) from exc
            if address.version != 4:
                raise ValueError(
                    f"{label} bind host currently supports IPv4 addresses only"
                )
            if address in TAILSCALE_IPV4_NETWORK:
                raise ValueError(
                    f"Do not persist a Tailscale IP in {key}. Same-host Chronicle "
                    "uses container DNS; keep the published port on 127.0.0.1."
                )
            if not address.is_loopback and not api_key:
                raise ValueError(
                    f"A non-loopback {label} bind requires LLAMA_API_KEY. "
                    "Set the same key in extras/llm-services/.env and "
                    "backends/advanced/.env."
                )
            return host, address

        bind_host, bind_ip = validated_host("LLM_BIND_HOST", "LLM")
        embed_bind_host, _ = validated_host("EMBED_BIND_HOST", "embedding")
        self.config["LLM_BIND_HOST"] = bind_host
        self.config["EMBED_BIND_HOST"] = embed_bind_host
        if api_key:
            self.config["LLAMA_API_KEY"] = api_key
        boundary = "loopback" if bind_ip.is_loopback else "authenticated non-loopback"
        self.console.print(
            f"[green][SUCCESS][/green] LLM host publication: {bind_host} ({boundary})"
        )

    def select_llm_model(self) -> tuple:
        """Select LLM chat model. Returns (repo, model_info)."""
        self.print_section("Chat Model Selection")

        configured_reference = getattr(self.args, "llm_model", None)
        if configured_reference:
            return self._llm_from_reference(configured_reference)

        model_choices = {}
        repos = list(LLM_MODELS.keys())
        current_repo = self._current_llm_repo()
        for i, (repo, info) in enumerate(LLM_MODELS.items(), 1):
            default_marker = ""
            if repo == current_repo:
                default_marker = " (Current)"
            elif current_repo is None and repo == DEFAULT_LLM_REPO:
                default_marker = " (Default)"
            model_choices[str(i)] = f"{info['description']}{default_marker}"

        custom_key = str(len(repos) + 1)
        model_choices[custom_key] = (
            "Custom GGUF (HuggingFace repo:quant, or local filename)"
        )

        # Prefer the currently served model; otherwise use the shipped default.
        preferred_repo = current_repo or DEFAULT_LLM_REPO
        default_choice = "1"
        for i, repo in enumerate(repos, 1):
            if repo == preferred_repo:
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
            if getattr(self.args, "ctx_size", None) in (None, ""):
                self.args.ctx_size = self.prompt_value(
                    "Context size", str(DEFAULT_CUSTOM_CONTEXT_WINDOW)
                )
            return self._llm_from_reference(ref)
        else:
            repo = repos[int(choice) - 1]
            info = dict(LLM_MODELS[repo])
            info["ctx_size"] = self._context_window(int(info["ctx_size"]))
            return repo, info

    def select_embed_model(self) -> tuple:
        """Select embedding model. Returns (repo, model_info)."""
        self.print_section("Embedding Model Selection")

        configured_reference = getattr(self.args, "embed_model", None)
        if configured_reference:
            return self._embed_from_reference(configured_reference)

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

        n_gpu_layers = getattr(self.args, "n_gpu_layers", None)
        if n_gpu_layers in (None, ""):
            n_gpu_layers = self.prompt_value(
                "GPU layers (-1 = all layers on GPU, 0 = CPU only)", "-1"
            )
        try:
            parsed_layers = int(n_gpu_layers)
        except (TypeError, ValueError) as exc:
            raise ValueError("GPU layers must be an integer") from exc
        if parsed_layers < -1:
            raise ValueError("GPU layers must be -1, 0, or a positive integer")
        self.config["N_GPU_LAYERS"] = str(parsed_layers)

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

    @staticmethod
    def _registry_llm_name(llm_repo, llm_info) -> str:
        """Map a served GGUF to the Chronicle registry entry that identifies it."""
        hf_ref = str(llm_info.get("hf") or "")
        upstream_repo = hf_ref.split(":", 1)[0]
        if llm_repo == QWEN36_REPO or upstream_repo == QWEN36_REPO:
            return "qwen36-llm"
        return "llamacpp-llm"

    @classmethod
    def _server_profile(cls, llm_repo, llm_info) -> Dict[str, str]:
        if cls._registry_llm_name(llm_repo, llm_info) == "qwen36-llm":
            return dict(QWEN_TEXT_SERVER_PROFILE)
        return dict(DEFAULT_SERVER_PROFILE)

    @staticmethod
    def _pi_max_tokens(context_window: int) -> int:
        return min(
            DEFAULT_PI_MAX_TOKENS,
            context_window // 4,
            context_window - PI_PROMPT_HEADROOM_TOKENS,
        )

    @classmethod
    def _reconcile_managed_pi_model(
        cls,
        config_manager: ConfigManager,
        *,
        registry_llm: str,
        context_window: int,
    ) -> bool:
        """Keep an active Pi agent aligned with the one locally served model.

        Chronicle's llama.cpp service hosts one chat model at a time. Retarget only
        the shipped managed llama.cpp entries; an explicit cloud/custom Pi model is
        left untouched.
        """
        config = config_manager.get_full_config()
        memory = config.get("memory")
        if not isinstance(memory, dict):
            return False
        agents = memory.get("agents")
        if not isinstance(agents, dict):
            return False
        write = agents.get("write") if isinstance(agents.get("write"), dict) else {}
        search = agents.get("search") if isinstance(agents.get("search"), dict) else {}
        active_backends = {
            str(write.get("backend") or "direct").lower(),
            str(search.get("backend") or "direct").lower(),
        }
        recovery = write.get("recovery_backend", "direct")
        if recovery not in (None, ""):
            active_backends.add(str(recovery).lower())
        if "pi" not in active_backends:
            return False

        backends = memory.setdefault("backends", {})
        if not isinstance(backends, dict):
            raise ValueError("memory.backends must be a mapping")
        pi = backends.setdefault("pi", {})
        if not isinstance(pi, dict):
            raise ValueError("memory.backends.pi must be a mapping")
        current_model = str(pi.get("model") or "").strip()
        if current_model and current_model not in MANAGED_LLAMACPP_REGISTRY_MODELS:
            return False

        pi["model"] = registry_llm
        pi["context_window"] = context_window
        pi["max_tokens"] = cls._pi_max_tokens(context_window)
        config_manager.save_full_config(config)
        return True

    def update_config_yml(self, llm_repo, llm_info):
        """Sync the selected llama.cpp model and make it the configured default."""
        config_manager = ConfigManager(service_path="extras/llm-services")
        registry_llm = self._registry_llm_name(llm_repo, llm_info)
        required_models = [registry_llm, "llamacpp-embed"]
        synced = config_manager.sync_models_from_defaults(required_models)
        missing = sorted(set(required_models) - set(synced))
        if missing:
            raise ValueError(
                "Could not sync required model definition(s) from defaults.yml: "
                + ", ".join(missing)
            )
        self.console.print(
            "[green][SUCCESS][/green] Re-synced model definitions from "
            f"defaults.yml: {', '.join(synced)}"
        )

        # llama.cpp exposes the exact --hf-repo/file identity through /v1/models.
        # Persist the selected identity/context/reasoning contract for every choice;
        # otherwise the registry and the one served model diverge.
        config = config_manager.get_full_config()
        selected = next(
            (
                model
                for model in (config.get("models") or [])
                if isinstance(model, dict) and model.get("name") == registry_llm
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"{registry_llm} was not available after syncing defaults.yml"
            )
        model_identity = llm_info.get("hf") or llm_info.get("file")
        if not str(model_identity or "").strip():
            raise ValueError(
                "Selected llama.cpp model has no HuggingFace or file identity"
            )
        context_window = self._positive_context_window(llm_info["ctx_size"])
        selected = dict(selected)
        selected["model_name"] = str(model_identity)
        selected["context_window"] = context_window
        selected["thinking"] = bool(llm_info.get("thinking", False))
        config_manager.add_or_update_model(selected)

        config_manager.update_config_defaults(
            {"llm": registry_llm, "embedding": "llamacpp-embed"}
        )
        reconciled_pi = self._reconcile_managed_pi_model(
            config_manager,
            registry_llm=registry_llm,
            context_window=context_window,
        )

        self.console.print(
            f"[green][SUCCESS][/green] Updated defaults.llm to '{registry_llm}' "
            "in config/config.yml"
        )
        self.console.print(
            "[green][SUCCESS][/green] Updated defaults.embedding to "
            "'llamacpp-embed' in config/config.yml"
        )
        if reconciled_pi:
            self.console.print(
                "[green][SUCCESS][/green] Aligned the active Pi memory backend with "
                f"{registry_llm} ({context_window}-token context)"
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
        table.add_row("Host Bind", self.config.get("LLM_BIND_HOST", "127.0.0.1"))
        table.add_row(
            "API Authentication",
            "enabled" if self.config.get("LLAMA_API_KEY") else "network boundary",
        )
        table.add_row("Context Size", str(self.config.get("CTX_SIZE", "8192")))
        table.add_row("Parallel Slots", self.config.get("LLAMA_ARG_N_PARALLEL", "1"))
        table.add_row(
            "Flash Attention", self.config.get("LLAMA_ARG_FLASH_ATTN", "auto")
        )
        table.add_row(
            "KV Cache",
            f"K={self.config.get('LLAMA_ARG_CACHE_TYPE_K', 'f16')}, "
            f"V={self.config.get('LLAMA_ARG_CACHE_TYPE_V', 'f16')}",
        )
        think_budget = str(self.config.get("LLAMA_ARG_THINK_BUDGET", "-1"))
        table.add_row(
            "Thinking Budget",
            "unrestricted" if think_budget == "-1" else f"{think_budget} tokens",
        )
        table.add_row(
            "Vision Projector",
            (
                "auto"
                if self.config.get("LLAMA_ARG_MMPROJ_AUTO", "true") == "true"
                else "disabled"
            ),
        )
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
        llm_host = self.config.get("LLM_BIND_HOST", "127.0.0.1")
        embed_host = self.config.get("EMBED_BIND_HOST", "127.0.0.1")

        def dial_host(bind_host: str) -> str:
            # Wildcard addresses describe where to listen, not where curl dials.
            return "127.0.0.1" if bind_host in {"0.0.0.0", "::", "[::]"} else bind_host

        llm_url = f"http://{dial_host(llm_host)}:{llm_port}"
        embed_url = f"http://{dial_host(embed_host)}:{embed_port}"
        self.console.print(f"   Chat:      [cyan]{llm_url}[/cyan]")
        self.console.print(f"   Embedding: [cyan]{embed_url}[/cyan]")
        self.console.print()
        self.console.print("3. Test health:")
        self.console.print(f"   [cyan]curl {llm_url}/health[/cyan]")
        self.console.print(f"   [cyan]curl {embed_url}/health[/cyan]")

    def run(self):
        """Run the complete setup process."""
        self.print_header("🧠 LLM Services Setup (llama.cpp)")
        self.console.print("Configure local LLM and embedding services via llama.cpp")
        self.console.print()

        try:
            # Select models
            llm_repo, llm_info = self.select_llm_model()
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
            self.config.update(self._server_profile(llm_repo, llm_info))
            self.configure_network_contract()

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
            self.update_config_yml(llm_repo, llm_info)

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
        help="Built-in model repo, HuggingFace repo:quant, or local GGUF filename",
    )
    parser.add_argument(
        "--embed-model",
        help="Built-in model repo, HuggingFace repo:quant, or local GGUF filename",
    )
    parser.add_argument(
        "--n-gpu-layers",
        help="Number of GPU layers (-1 = all)",
    )
    parser.add_argument(
        "--ctx-size",
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
