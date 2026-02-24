#!/usr/bin/env python3
"""
Chronicle Advanced Backend Interactive Setup Script
Interactive configuration for all services and API keys
"""

import argparse
import os
import platform
import secrets
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
from config_manager import ConfigManager
from setup_utils import detect_tailscale_info, mask_value
from setup_utils import prompt_password as util_prompt_password
from setup_utils import prompt_with_existing_masked, read_env_value


class ChronicleSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()
        self.config_yml_path = Path(
            "../../config/config.yml"
        )  # Main config at config/config.yml

        # Check if we're in the right directory
        if not Path("pyproject.toml").exists() or not Path("src").exists():
            self.console.print(
                "[red][ERROR][/red] Please run this script from the backends/advanced directory"
            )
            sys.exit(1)

        # Initialize ConfigManager (single source of truth for config.yml)
        self.config_manager = ConfigManager(service_path="backends/advanced")
        self.console.print(
            f"[blue][INFO][/blue] Using config.yml at: {self.config_manager.config_yml_path}"
        )

        # Verify config.yml exists - fail fast if missing
        if not self.config_manager.config_yml_path.exists():
            self.console.print(
                f"[red][ERROR][/red] config.yml not found at {self.config_manager.config_yml_path}"
            )
            self.console.print(
                "[red][ERROR][/red] Run wizard.py from project root to create config.yml"
            )
            sys.exit(1)

        # Ensure plugins.yml exists (copy from template if missing)
        self._ensure_plugins_yml_exists()

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
            # Always provide a default to avoid EOF issues
            return Prompt.ask(prompt, default=default)
        except EOFError:
            self.console.print(f"Using default: {default}")
            return default

    def prompt_password(self, prompt: str) -> str:
        """Prompt for password (delegates to shared utility)"""
        return util_prompt_password(prompt, min_length=8, allow_generated=True)

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

    def _ensure_plugins_yml_exists(self):
        """Ensure plugins.yml exists by copying from template if missing."""
        plugins_yml = Path("../../config/plugins.yml")
        plugins_template = Path("../../config/plugins.yml.template")

        if not plugins_yml.exists():
            if plugins_template.exists():
                self.console.print(
                    "[blue][INFO][/blue] plugins.yml not found, creating from template..."
                )
                shutil.copy2(plugins_template, plugins_yml)
                self.console.print(
                    f"[green]✅[/green] Created {plugins_yml} from template"
                )
                self.console.print(
                    "[yellow][NOTE][/yellow] Edit config/plugins.yml to configure plugins"
                )
                self.console.print(
                    "[yellow][NOTE][/yellow] Set HA_TOKEN in .env for Home Assistant integration"
                )
            else:
                raise RuntimeError(
                    f"Template file not found: {plugins_template}\n"
                    f"The repository structure is incomplete. Please ensure config/plugins.yml.template exists."
                )
        else:
            self.console.print(f"[blue][INFO][/blue] Found existing {plugins_yml}")

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

    def read_existing_env_value(self, key: str) -> str:
        """Read a value from existing .env file (delegates to shared utility)"""
        return read_env_value(".env", key)

    def mask_api_key(self, key: str, show_chars: int = 5) -> str:
        """Mask API key (delegates to shared utility)"""
        return mask_value(key, show_chars)

    def prompt_with_existing_masked(
        self,
        prompt_text: str,
        env_key: str,
        placeholders: list,
        is_password: bool = False,
        default: str = "",
    ) -> str:
        """
        Prompt for a value, showing masked existing value from .env if present.
        Delegates to shared utility from setup_utils.

        Args:
            prompt_text: The prompt to display
            env_key: The .env key to check for existing value
            placeholders: List of placeholder values to treat as "not set"
            is_password: Whether to mask the value (for passwords/tokens)
            default: Default value if no existing value

        Returns:
            User input value, existing value if reused, or default
        """
        # Use shared utility with auto-read from .env
        return prompt_with_existing_masked(
            prompt_text=prompt_text,
            env_file_path=".env",
            env_key=env_key,
            placeholders=placeholders,
            is_password=is_password,
            default=default,
        )

    def setup_authentication(self):
        """Configure authentication settings"""
        self.print_section("Authentication Setup")
        self.console.print("Configure admin account for the dashboard")
        self.console.print()

        # Read existing values for re-run support
        existing_email = self.read_existing_env_value("ADMIN_EMAIL")
        default_email = existing_email if existing_email else "admin@example.com"
        self.config["ADMIN_EMAIL"] = self.prompt_value("Admin email", default_email)

        # Allow reusing existing admin password
        existing_password = self.read_existing_env_value("ADMIN_PASSWORD")
        if existing_password:
            password = prompt_with_existing_masked(
                prompt_text="Admin password (min 8 chars)",
                existing_value=existing_password,
                is_password=True,
            )
            self.config["ADMIN_PASSWORD"] = password
        else:
            self.config["ADMIN_PASSWORD"] = self.prompt_password(
                "Admin password (min 8 chars)"
            )

        # Preserve existing AUTH_SECRET_KEY to avoid invalidating JWTs
        existing_secret = self.read_existing_env_value("AUTH_SECRET_KEY")
        if existing_secret:
            self.config["AUTH_SECRET_KEY"] = existing_secret
            self.console.print(
                "[blue][INFO][/blue] Reusing existing AUTH_SECRET_KEY (existing JWT tokens remain valid)"
            )
        else:
            self.config["AUTH_SECRET_KEY"] = secrets.token_hex(32)

        self.console.print("[green][SUCCESS][/green] Admin account configured")

    def setup_transcription(self):
        """Configure transcription provider - updates config.yml and .env"""
        # Check if transcription provider was provided via command line
        if (
            hasattr(self.args, "transcription_provider")
            and self.args.transcription_provider
        ):
            provider = self.args.transcription_provider
            self.console.print(
                f"[green]✅[/green] Transcription: {provider} (configured via wizard)"
            )

            # Map provider to choice
            if provider == "deepgram":
                choice = "1"
            elif provider == "parakeet":
                choice = "2"
            elif provider == "vibevoice":
                choice = "3"
            elif provider == "qwen3-asr":
                choice = "4"
            elif provider == "smallest":
                choice = "5"
            elif provider == "none":
                choice = "6"
            else:
                choice = "1"  # Default to Deepgram
        else:
            self.print_section("Speech-to-Text Configuration")

            self.console.print(
                "[blue][INFO][/blue] Provider selection is configured in config.yml (defaults.stt)"
            )
            self.console.print("[blue][INFO][/blue] API keys are stored in .env")
            self.console.print()

            # Interactive prompt
            is_macos = platform.system() == "Darwin"

            if is_macos:
                parakeet_desc = "Offline (Parakeet ASR - CPU-based, runs locally)"
                vibevoice_desc = "Offline (VibeVoice - CPU-based, built-in diarization)"
            else:
                parakeet_desc = "Offline (Parakeet ASR - GPU recommended, runs locally)"
                vibevoice_desc = (
                    "Offline (VibeVoice - GPU recommended, built-in diarization)"
                )

            qwen3_desc = (
                "Offline (Qwen3-ASR - GPU required, 52 languages, streaming + batch)"
            )

            smallest_desc = "Smallest.ai Pulse (cloud-based, fast, requires API key)"

            choices = {
                "1": "Deepgram (recommended - high quality, cloud-based)",
                "2": parakeet_desc,
                "3": vibevoice_desc,
                "4": qwen3_desc,
                "5": smallest_desc,
                "6": "None (skip transcription setup)",
            }

            choice = self.prompt_choice(
                "Choose your transcription provider:", choices, "1"
            )

        if choice == "1":
            self.console.print("[blue][INFO][/blue] Deepgram selected")
            self.console.print("Get your API key from: https://console.deepgram.com/")

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="Deepgram API key (leave empty to skip)",
                env_key="DEEPGRAM_API_KEY",
                placeholders=["your_deepgram_api_key_here", "your-deepgram-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                # Write API key to .env
                self.config["DEEPGRAM_API_KEY"] = api_key

                # Update config.yml to use Deepgram
                self.config_manager.update_config_defaults({"stt": "stt-deepgram"})

                self.console.print(
                    "[green][SUCCESS][/green] Deepgram configured in config.yml and .env"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-deepgram")
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - transcription will not work"
                )

        elif choice == "2":
            self.console.print("[blue][INFO][/blue] Offline Parakeet ASR selected")
            parakeet_url = self.prompt_value(
                "Parakeet ASR URL (without http:// prefix)", "host.docker.internal:8767"
            )

            # Write URL to .env for ${PARAKEET_ASR_URL} placeholder in config.yml
            self.config["PARAKEET_ASR_URL"] = parakeet_url

            # Update config.yml to use Parakeet
            self.config_manager.update_config_defaults({"stt": "stt-parakeet-batch"})

            self.console.print(
                "[green][SUCCESS][/green] Parakeet configured in config.yml and .env"
            )
            self.console.print(
                "[blue][INFO][/blue] Set defaults.stt: stt-parakeet-batch"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Parakeet service: cd ../../extras/asr-services && docker compose up nemo-asr"
            )

        elif choice == "3":
            self.console.print(
                "[blue][INFO][/blue] Offline VibeVoice ASR selected (built-in speaker diarization)"
            )
            vibevoice_url = self.prompt_value(
                "VibeVoice ASR URL (without http:// prefix)",
                "host.docker.internal:8767",
            )

            # Write URL to .env for ${VIBEVOICE_ASR_URL} placeholder in config.yml
            self.config["VIBEVOICE_ASR_URL"] = vibevoice_url

            # Update config.yml to use VibeVoice
            self.config_manager.update_config_defaults({"stt": "stt-vibevoice"})

            self.console.print(
                "[green][SUCCESS][/green] VibeVoice configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-vibevoice")
            self.console.print(
                "[blue][INFO][/blue] VibeVoice provides built-in speaker diarization - pyannote will be skipped"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start VibeVoice service: cd ../../extras/asr-services && docker compose up vibevoice-asr"
            )

        elif choice == "4":
            self.console.print(
                "[blue][INFO][/blue] Qwen3-ASR selected (52 languages, streaming + batch via vLLM)"
            )
            qwen3_url = self.prompt_value(
                "Qwen3-ASR URL", "http://host.docker.internal:8767"
            )

            # Write URL to .env for ${QWEN3_ASR_URL} placeholder in config.yml
            self.config["QWEN3_ASR_URL"] = qwen3_url.replace("http://", "").rstrip("/")

            # Also set streaming URL (same host, port 8769)
            stream_host = qwen3_url.replace("http://", "").split(":")[0]
            self.config["QWEN3_ASR_STREAM_URL"] = f"{stream_host}:8769"

            # Update config.yml to use Qwen3-ASR
            self.config_manager.update_config_defaults({"stt": "stt-qwen3-asr"})

            self.console.print(
                "[green][SUCCESS][/green] Qwen3-ASR configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-qwen3-asr")
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Qwen3-ASR: cd ../../extras/asr-services && docker compose up qwen3-asr-wrapper qwen3-asr-bridge -d"
            )

        elif choice == "5":
            self.console.print("[blue][INFO][/blue] Smallest.ai Pulse selected")
            self.console.print("Get your API key from: https://smallest.ai/")

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="Smallest.ai API key (leave empty to skip)",
                env_key="SMALLEST_API_KEY",
                placeholders=["your_smallest_api_key_here", "your-smallest-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                # Write API key to .env
                self.config["SMALLEST_API_KEY"] = api_key

                # Update config.yml to use Smallest.ai (batch + streaming)
                self.config_manager.update_config_defaults(
                    {"stt": "stt-smallest", "stt_stream": "stt-smallest-stream"}
                )

                self.console.print(
                    "[green][SUCCESS][/green] Smallest.ai configured in config.yml and .env"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-smallest")
                self.console.print(
                    "[blue][INFO][/blue] Set defaults.stt_stream: stt-smallest-stream"
                )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - transcription will not work"
                )

        elif choice == "6":
            self.console.print("[blue][INFO][/blue] Skipping transcription setup")

    def setup_streaming_provider(self):
        """Configure a separate streaming provider if --streaming-provider was passed.

        When a different streaming provider is specified, sets defaults.stt_stream
        and enables always_batch_retranscribe (batch provider was set by setup_transcription).
        """
        if (
            not hasattr(self.args, "streaming_provider")
            or not self.args.streaming_provider
        ):
            return

        streaming_provider = self.args.streaming_provider
        self.console.print(
            f"\n[green]✅[/green] Streaming provider: {streaming_provider} (configured via wizard)"
        )

        # Map streaming provider to stt_stream config value
        provider_to_stt_stream = {
            "deepgram": "stt-deepgram-stream",
            "smallest": "stt-smallest-stream",
            "qwen3-asr": "stt-qwen3-asr",
        }

        stream_stt = provider_to_stt_stream.get(streaming_provider)
        if not stream_stt:
            self.console.print(
                f"[yellow][WARNING][/yellow] Unknown streaming provider: {streaming_provider}"
            )
            return

        # Set stt_stream (batch stt was already set by setup_transcription)
        self.config_manager.update_config_defaults({"stt_stream": stream_stt})

        # Enable always_batch_retranscribe
        full_config = self.config_manager.get_full_config()
        if "backend" not in full_config:
            full_config["backend"] = {}
        if "transcription" not in full_config["backend"]:
            full_config["backend"]["transcription"] = {}
        full_config["backend"]["transcription"]["always_batch_retranscribe"] = True
        self.config_manager.save_full_config(full_config)

        self.console.print(f"[blue][INFO][/blue] Set defaults.stt_stream: {stream_stt}")
        self.console.print(f"[blue][INFO][/blue] Enabled always_batch_retranscribe")

        # Prompt for streaming provider env vars if not already set
        if streaming_provider == "deepgram":
            existing_key = read_env_value(".env", "DEEPGRAM_API_KEY")
            if not existing_key or existing_key in (
                "your_deepgram_api_key_here",
                "your-deepgram-key-here",
            ):
                api_key = self.prompt_with_existing_masked(
                    prompt_text="Deepgram API key for streaming",
                    env_key="DEEPGRAM_API_KEY",
                    placeholders=[
                        "your_deepgram_api_key_here",
                        "your-deepgram-key-here",
                    ],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["DEEPGRAM_API_KEY"] = api_key
        elif streaming_provider == "smallest":
            existing_key = read_env_value(".env", "SMALLEST_API_KEY")
            if not existing_key or existing_key in (
                "your_smallest_api_key_here",
                "your-smallest-key-here",
            ):
                api_key = self.prompt_with_existing_masked(
                    prompt_text="Smallest.ai API key for streaming",
                    env_key="SMALLEST_API_KEY",
                    placeholders=[
                        "your_smallest_api_key_here",
                        "your-smallest-key-here",
                    ],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["SMALLEST_API_KEY"] = api_key
        elif streaming_provider == "qwen3-asr":
            existing_url = read_env_value(".env", "QWEN3_ASR_STREAM_URL")
            if not existing_url:
                qwen3_url = self.prompt_value(
                    "Qwen3-ASR streaming URL", "http://host.docker.internal:8769"
                )
                stream_host = qwen3_url.replace("http://", "").rstrip("/")
                self.config["QWEN3_ASR_STREAM_URL"] = stream_host

    def setup_llm(self):
        """Configure LLM provider - updates config.yml and .env"""
        self.print_section("LLM Provider Configuration")

        self.console.print(
            "[blue][INFO][/blue] LLM configuration will be saved to config.yml"
        )
        self.console.print()

        choices = {
            "1": "OpenAI (GPT-4, GPT-3.5 - requires API key)",
            "2": "Ollama (local models - runs locally)",
            "3": "OpenAI-Compatible (custom endpoint - Groq, Together AI, LM Studio, etc.)",
            "4": "Skip (no memory extraction)",
        }

        choice = self.prompt_choice("Which LLM provider will you use?", choices, "1")

        if choice == "1":
            self.console.print("[blue][INFO][/blue] OpenAI selected")
            self.console.print(
                "Get your API key from: https://platform.openai.com/api-keys"
            )

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="OpenAI API key (leave empty to skip)",
                env_key="OPENAI_API_KEY",
                placeholders=["your_openai_api_key_here", "your-openai-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                self.config["OPENAI_API_KEY"] = api_key
                # Update config.yml to use OpenAI models
                self.config_manager.update_config_defaults(
                    {"llm": "openai-llm", "embedding": "openai-embed"}
                )
                self.console.print(
                    "[green][SUCCESS][/green] OpenAI configured in config.yml"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.llm: openai-llm")
                self.console.print(
                    "[blue][INFO][/blue] Set defaults.embedding: openai-embed"
                )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - memory extraction will not work"
                )

        elif choice == "2":
            self.console.print("[blue][INFO][/blue] Ollama selected")
            # Update config.yml to use Ollama models
            self.config_manager.update_config_defaults(
                {"llm": "local-llm", "embedding": "local-embed"}
            )
            self.console.print(
                "[green][SUCCESS][/green] Ollama configured in config.yml"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.llm: local-llm")
            self.console.print(
                "[blue][INFO][/blue] Set defaults.embedding: local-embed"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Make sure Ollama is running and models are pulled"
            )

        elif choice == "3":
            self.console.print(
                "[blue][INFO][/blue] OpenAI-Compatible custom endpoint selected"
            )
            self.console.print(
                "This works with any provider that exposes an OpenAI-compatible API"
            )
            self.console.print("(e.g., Groq, Together AI, LM Studio, vLLM, etc.)")
            self.console.print()

            # Prompt for base URL (required)
            base_url = self.prompt_value(
                "API Base URL (e.g., https://api.groq.com/openai/v1)", ""
            )
            if not base_url:
                self.console.print(
                    "[yellow][WARNING][/yellow] No base URL provided - skipping custom LLM setup"
                )
            else:
                # Prompt for API key
                api_key = self.prompt_with_existing_masked(
                    prompt_text="API Key (leave empty if not required)",
                    env_key="CUSTOM_LLM_API_KEY",
                    placeholders=["your_custom_llm_api_key_here"],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["CUSTOM_LLM_API_KEY"] = api_key

                # Prompt for model name (required)
                model_name = self.prompt_value(
                    "LLM Model name (e.g., llama-3.1-70b-versatile)", ""
                )
                if not model_name:
                    self.console.print(
                        "[yellow][WARNING][/yellow] No model name provided - skipping custom LLM setup"
                    )
                else:
                    # Create LLM model entry
                    llm_model = {
                        "name": "custom-llm",
                        "description": "Custom OpenAI-compatible LLM",
                        "model_type": "llm",
                        "model_provider": "openai",
                        "api_family": "openai",
                        "model_name": model_name,
                        "model_url": base_url,
                        "api_key": "${oc.env:CUSTOM_LLM_API_KEY,''}",
                        "model_params": {"temperature": 0.2, "max_tokens": 2000},
                        "model_output": "json",
                    }
                    self.config_manager.add_or_update_model(llm_model)

                    # Prompt for optional embedding model
                    embedding_model_name = self.prompt_value(
                        "Embedding model name (leave empty to use Ollama local-embed)",
                        "",
                    )

                    if embedding_model_name:
                        embed_dim_str = self.prompt_value(
                            "Embedding dimensions (e.g. 1536 for text-embedding-3-small, 3072 for text-embedding-3-large)",
                            "1536",
                        )
                        try:
                            embedding_dimensions = int(embed_dim_str)
                        except ValueError:
                            self.console.print(
                                f"[yellow][WARNING][/yellow] Invalid dimensions '{embed_dim_str}', using default 1536"
                            )
                            raise ValueError(f"Invalid dimensions '{embed_dim_str}'")

                        embed_model = {
                            "name": "custom-embed",
                            "description": "Custom OpenAI-compatible embeddings",
                            "model_type": "embedding",
                            "model_provider": "openai",
                            "api_family": "openai",
                            "model_name": embedding_model_name,
                            "model_url": base_url,
                            "api_key": "${oc.env:CUSTOM_LLM_API_KEY,''}",
                            "embedding_dimensions": embedding_dimensions,
                            "model_output": "vector",
                        }
                        self.config_manager.add_or_update_model(embed_model)
                        self.config_manager.update_config_defaults(
                            {"llm": "custom-llm", "embedding": "custom-embed"}
                        )
                        self.console.print(
                            "[green][SUCCESS][/green] Custom LLM and embedding configured in config.yml"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.llm: custom-llm"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.embedding: custom-embed"
                        )
                    else:
                        self.config_manager.update_config_defaults(
                            {"llm": "custom-llm", "embedding": "local-embed"}
                        )
                        self.console.print(
                            "[green][SUCCESS][/green] Custom LLM configured in config.yml"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.llm: custom-llm"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.embedding: local-embed (Ollama)"
                        )
                        self.console.print(
                            "[yellow][WARNING][/yellow] Make sure Ollama is running for embeddings"
                        )

        elif choice == "4":
            self.console.print(
                "[blue][INFO][/blue] Skipping LLM setup - memory extraction disabled"
            )
            # Disable memory extraction in config.yml
            self.config_manager.update_memory_config({"extraction": {"enabled": False}})

    def setup_memory(self):
        """Configure memory provider - updates config.yml"""
        self.print_section("Memory Storage Configuration")

        choices = {
            "1": "Chronicle Native (Qdrant + custom extraction)",
            "2": "OpenMemory MCP (cross-client compatible, external server)",
        }

        choice = self.prompt_choice("Choose your memory storage backend:", choices, "1")

        if choice == "1":
            self.console.print(
                "[blue][INFO][/blue] Chronicle Native memory provider selected"
            )

            qdrant_url = self.prompt_value("Qdrant URL", "qdrant")
            self.config["QDRANT_BASE_URL"] = qdrant_url

            # Update config.yml (also updates .env automatically)
            self.config_manager.update_memory_config({"provider": "chronicle"})
            self.console.print(
                "[green][SUCCESS][/green] Chronicle memory provider configured in config.yml and .env"
            )

        elif choice == "2":
            self.console.print("[blue][INFO][/blue] OpenMemory MCP selected")

            mcp_url = self.prompt_value(
                "OpenMemory MCP server URL", "http://host.docker.internal:8765"
            )
            client_name = self.prompt_value("OpenMemory client name", "chronicle")
            user_id = self.prompt_value("OpenMemory user ID", "openmemory")
            timeout = self.prompt_value("OpenMemory timeout (seconds)", "30")

            # Update config.yml with OpenMemory MCP settings (also updates .env automatically)
            self.config_manager.update_memory_config(
                {
                    "provider": "openmemory_mcp",
                    "openmemory_mcp": {
                        "server_url": mcp_url,
                        "client_name": client_name,
                        "user_id": user_id,
                        "timeout": int(timeout),
                    },
                }
            )
            self.console.print(
                "[green][SUCCESS][/green] OpenMemory MCP configured in config.yml and .env"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start OpenMemory: cd ../../extras/openmemory-mcp && docker compose up -d"
            )

    def setup_optional_services(self):
        """Configure optional services"""
        # Check if speaker service URL provided via args
        has_speaker_arg = (
            hasattr(self.args, "speaker_service_url") and self.args.speaker_service_url
        )
        has_asr_arg = (
            hasattr(self.args, "parakeet_asr_url") and self.args.parakeet_asr_url
        )

        if has_speaker_arg:
            self.config["SPEAKER_SERVICE_URL"] = self.args.speaker_service_url
            self.console.print(
                f"[green]✅[/green] Speaker Recognition: {self.args.speaker_service_url} (configured via wizard)"
            )

        if has_asr_arg:
            self.config["PARAKEET_ASR_URL"] = self.args.parakeet_asr_url
            self.console.print(
                f"[green]✅[/green] Parakeet ASR: {self.args.parakeet_asr_url} (configured via wizard)"
            )

        # Only show interactive section if not all configured via args
        if not has_speaker_arg:
            try:
                enable_speaker = Confirm.ask(
                    "Enable Speaker Recognition?", default=False
                )
            except EOFError:
                self.console.print("Using default: No")
                enable_speaker = False

            if enable_speaker:
                speaker_url = self.prompt_value(
                    "Speaker Recognition service URL",
                    "http://host.docker.internal:8001",
                )
                self.config["SPEAKER_SERVICE_URL"] = speaker_url
                self.console.print(
                    "[green][SUCCESS][/green] Speaker Recognition configured"
                )
                self.console.print(
                    "[blue][INFO][/blue] Start with: cd ../../extras/speaker-recognition && docker compose up -d"
                )

        # Check if Tailscale auth key provided via args
        if hasattr(self.args, "ts_authkey") and self.args.ts_authkey:
            self.config["TS_AUTHKEY"] = self.args.ts_authkey
            self.console.print(
                f"[green][SUCCESS][/green] Tailscale auth key configured (Docker integration enabled)"
            )

    def setup_neo4j(self):
        """Configure Neo4j credentials (always required - used by Knowledge Graph)"""
        neo4j_password = getattr(self.args, "neo4j_password", None)

        if neo4j_password:
            self.console.print(
                f"[green]✅[/green] Neo4j: password configured via wizard"
            )
        else:
            # Interactive prompt (standalone init.py run)
            self.console.print()
            self.console.print("[bold cyan]Neo4j Configuration[/bold cyan]")
            self.console.print(
                "Neo4j is used for Knowledge Graph (entity/relationship extraction)"
            )
            self.console.print()
            neo4j_password = self.prompt_with_existing_masked(
                "Neo4j password (min 8 chars)",
                env_key="NEO4J_PASSWORD",
                placeholders=["", "your-neo4j-password"],
                is_password=True,
                default="neo4jpassword",
            )

        self.config["NEO4J_HOST"] = "neo4j"
        self.config["NEO4J_USER"] = "neo4j"
        self.config["NEO4J_PASSWORD"] = neo4j_password
        self.console.print("[green][SUCCESS][/green] Neo4j credentials configured")

    def setup_obsidian(self):
        """Configure Obsidian integration (optional feature flag only - Neo4j credentials handled by setup_neo4j)"""
        if hasattr(self.args, "enable_obsidian") and self.args.enable_obsidian:
            enable_obsidian = True
            self.console.print(
                f"[green]✅[/green] Obsidian: enabled (configured via wizard)"
            )
        else:
            # Interactive prompt (fallback)
            self.console.print()
            self.console.print("[bold cyan]Obsidian Integration (Optional)[/bold cyan]")
            self.console.print(
                "Enable graph-based knowledge management for Obsidian vault notes"
            )
            self.console.print()

            try:
                enable_obsidian = Confirm.ask(
                    "Enable Obsidian integration?", default=False
                )
            except EOFError:
                self.console.print("Using default: No")
                enable_obsidian = False

        if enable_obsidian:
            self.config_manager.update_memory_config(
                {"obsidian": {"enabled": True, "neo4j_host": "neo4j", "timeout": 30}}
            )
            self.console.print("[green][SUCCESS][/green] Obsidian integration enabled")
        else:
            self.config_manager.update_memory_config(
                {"obsidian": {"enabled": False, "neo4j_host": "neo4j", "timeout": 30}}
            )
            self.console.print("[blue][INFO][/blue] Obsidian integration disabled")

    def setup_knowledge_graph(self):
        """Configure Knowledge Graph (Neo4j-based entity/relationship extraction - enabled by default)"""
        if (
            hasattr(self.args, "enable_knowledge_graph")
            and self.args.enable_knowledge_graph
        ):
            enable_kg = True
        else:
            self.console.print()
            self.console.print(
                "[bold cyan]Knowledge Graph (Entity Extraction)[/bold cyan]"
            )
            self.console.print(
                "Extract people, places, organizations, events, and tasks from conversations"
            )
            self.console.print()

            try:
                enable_kg = Confirm.ask("Enable Knowledge Graph?", default=True)
            except EOFError:
                self.console.print("Using default: Yes")
                enable_kg = True

        if enable_kg:
            self.config_manager.update_memory_config(
                {
                    "knowledge_graph": {
                        "enabled": True,
                        "neo4j_host": "neo4j",
                        "timeout": 30,
                    }
                }
            )
            self.console.print("[green][SUCCESS][/green] Knowledge Graph enabled")
            self.console.print(
                "[blue][INFO][/blue] Entities and relationships will be extracted from conversations"
            )
        else:
            self.config_manager.update_memory_config(
                {
                    "knowledge_graph": {
                        "enabled": False,
                        "neo4j_host": "neo4j",
                        "timeout": 30,
                    }
                }
            )
            self.console.print("[blue][INFO][/blue] Knowledge Graph disabled")

    def setup_langfuse(self):
        """Configure LangFuse observability and prompt management"""
        self.console.print()
        self.console.print(
            "[bold cyan]LangFuse Observability & Prompt Management[/bold cyan]"
        )

        # Check if keys were passed from wizard (langfuse init already ran)
        langfuse_pub = getattr(self.args, "langfuse_public_key", None)
        langfuse_sec = getattr(self.args, "langfuse_secret_key", None)

        if langfuse_pub and langfuse_sec:
            # Auto-configure from wizard — no prompts needed
            langfuse_host = (
                getattr(self.args, "langfuse_host", None) or "http://langfuse-web:3000"
            )
            self.config["LANGFUSE_HOST"] = langfuse_host
            self.config["LANGFUSE_PUBLIC_KEY"] = langfuse_pub
            self.config["LANGFUSE_SECRET_KEY"] = langfuse_sec
            self.config["LANGFUSE_BASE_URL"] = langfuse_host

            # Derive browser-accessible URL for deep-links
            public_url = (
                getattr(self.args, "langfuse_public_url", None)
                or "http://localhost:3002"
            )
            self._save_langfuse_public_url(public_url)

            source = "external" if "langfuse-web" not in langfuse_host else "local"
            self.console.print(
                f"[green][SUCCESS][/green] LangFuse auto-configured ({source})"
            )
            self.console.print(f"[blue][INFO][/blue] Host: {langfuse_host}")
            self.console.print(f"[blue][INFO][/blue] Public URL: {public_url}")
            self.console.print(
                f"[blue][INFO][/blue] Public key: {self.mask_api_key(langfuse_pub)}"
            )
            return

        # Manual configuration (standalone init.py run)
        self.console.print(
            "Enable LLM tracing, observability, and prompt management with LangFuse"
        )
        self.console.print(
            "Self-host: cd ../../extras/langfuse && docker compose up -d"
        )
        self.console.print()

        try:
            enable_langfuse = Confirm.ask("Enable LangFuse?", default=False)
        except EOFError:
            self.console.print("Using default: No")
            enable_langfuse = False

        if enable_langfuse:
            host = self.prompt_with_existing_masked(
                prompt_text="LangFuse host URL",
                env_key="LANGFUSE_HOST",
                placeholders=[""],
                is_password=False,
                default="http://langfuse-web:3000",
            )
            public_key = self.prompt_with_existing_masked(
                prompt_text="LangFuse public key",
                env_key="LANGFUSE_PUBLIC_KEY",
                placeholders=[""],
                is_password=False,
                default="",
            )
            secret_key = self.prompt_with_existing_masked(
                prompt_text="LangFuse secret key",
                env_key="LANGFUSE_SECRET_KEY",
                placeholders=[""],
                is_password=True,
                default="",
            )

            if host:
                self.config["LANGFUSE_HOST"] = host
                self.config["LANGFUSE_BASE_URL"] = host
            if public_key:
                self.config["LANGFUSE_PUBLIC_KEY"] = public_key
            if secret_key:
                self.config["LANGFUSE_SECRET_KEY"] = secret_key

            # Browser-accessible URL for deep-links (stored in config.yml, not .env)
            public_url = Prompt.ask(
                "LangFuse browser URL (for dashboard links)",
                default="http://localhost:3002",
            )
            if public_url:
                self._save_langfuse_public_url(public_url)

            self.console.print("[green][SUCCESS][/green] LangFuse configured")
        else:
            self.console.print("[blue][INFO][/blue] LangFuse disabled")

    def _save_langfuse_public_url(self, public_url: str):
        """Save the Langfuse browser-accessible URL to config.yml."""
        full_config = self.config_manager.get_full_config()
        if "observability" not in full_config:
            full_config["observability"] = {}
        if "langfuse" not in full_config["observability"]:
            full_config["observability"]["langfuse"] = {}
        full_config["observability"]["langfuse"]["public_url"] = public_url
        self.config_manager.save_full_config(full_config)

    def setup_network(self):
        """Configure network settings"""
        self.print_section("Network Configuration")

        self.config["BACKEND_PUBLIC_PORT"] = self.prompt_value("Backend port", "8000")
        self.config["WEBUI_PORT"] = self.prompt_value("Web UI port", "5173")

    def setup_https(self):
        """Configure HTTPS settings for microphone access"""
        # Check if HTTPS configuration provided via command line
        if hasattr(self.args, "enable_https") and self.args.enable_https:
            enable_https = True
            server_ip = getattr(self.args, "server_ip", "localhost")
            self.console.print(
                f"[green]✅[/green] HTTPS: {server_ip} (configured via wizard)"
            )
        else:
            # Interactive configuration
            self.print_section("HTTPS Configuration (Optional)")

            try:
                enable_https = Confirm.ask(
                    "Enable HTTPS for microphone access?", default=False
                )
            except EOFError:
                self.console.print("Using default: No")
                enable_https = False

            if enable_https:
                self.console.print(
                    "[blue][INFO][/blue] HTTPS enables microphone access in browsers"
                )

                # Try to auto-detect Tailscale address
                ts_dns, ts_ip = detect_tailscale_info()

                if ts_dns:
                    self.console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale DNS: {ts_dns}"
                    )
                    if ts_ip:
                        self.console.print(
                            f"[green][AUTO-DETECTED][/green] Tailscale IP:  {ts_ip}"
                        )
                    default_address = ts_dns
                elif ts_ip:
                    self.console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale IP: {ts_ip}"
                    )
                    default_address = ts_ip
                else:
                    self.console.print("[blue][INFO][/blue] Tailscale not detected")
                    self.console.print(
                        "[blue][INFO][/blue] To find your Tailscale address: tailscale status --json | jq -r '.Self.DNSName'"
                    )
                    default_address = "localhost"

                self.console.print(
                    "[blue][INFO][/blue] For local-only access, use 'localhost'"
                )

                # Use the new masked prompt function (not masked for IP, but shows existing)
                server_ip = self.prompt_with_existing_masked(
                    prompt_text="Server IP/Domain for SSL certificate",
                    env_key="SERVER_IP",
                    placeholders=["localhost", "your-server-ip-here"],
                    is_password=False,
                    default=default_address,
                )

        if enable_https:

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
            caddyfile_template = script_dir / "Caddyfile.template"
            caddyfile_path = script_dir / "Caddyfile"

            if caddyfile_template.exists():
                try:
                    # Check if Caddyfile exists as a directory (common issue)
                    if caddyfile_path.exists() and caddyfile_path.is_dir():
                        self.console.print(
                            "[red]❌ ERROR: 'Caddyfile' exists as a directory![/red]"
                        )
                        self.console.print(
                            "[yellow]   Please remove it manually:[/yellow]"
                        )
                        self.console.print(
                            f"[yellow]   rm -rf {caddyfile_path}[/yellow]"
                        )
                        self.console.print(
                            "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                        )
                        self.config["HTTPS_ENABLED"] = "false"
                    else:
                        with open(caddyfile_template, "r") as f:
                            caddyfile_content = f.read()

                        # Replace TAILSCALE_IP with server_ip
                        caddyfile_content = caddyfile_content.replace(
                            "TAILSCALE_IP", server_ip
                        )

                        with open(caddyfile_path, "w") as f:
                            f.write(caddyfile_content)

                        self.console.print(
                            f"[green][SUCCESS][/green] Caddyfile created for: {server_ip}"
                        )
                        self.config["HTTPS_ENABLED"] = "true"
                        self.config["SERVER_IP"] = server_ip

                except Exception as e:
                    self.console.print(
                        f"[red]❌ ERROR: Caddyfile generation failed: {e}[/red]"
                    )
                    self.console.print(
                        "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                    )
                    self.config["HTTPS_ENABLED"] = "false"
            else:
                self.console.print("[red]❌ ERROR: Caddyfile.template not found[/red]")
                self.console.print(
                    "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                )
                self.config["HTTPS_ENABLED"] = "false"
        else:
            self.config["HTTPS_ENABLED"] = "false"

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

        # Note: config.yml is automatically saved by ConfigManager when updates are made
        self.console.print(
            "[blue][INFO][/blue] Configuration saved to config.yml and .env (via ConfigManager)"
        )

    def copy_config_templates(self):
        """Copy other configuration files"""

        if (
            not Path("diarization_config.json").exists()
            and Path("diarization_config.json.template").exists()
        ):
            shutil.copy2("diarization_config.json.template", "diarization_config.json")
            self.console.print(
                "[green][SUCCESS][/green] diarization_config.json created"
            )

    def show_summary(self):
        """Show configuration summary"""
        self.print_section("Configuration Summary")
        self.console.print()

        self.console.print(
            f"✅ Admin Account: {self.config.get('ADMIN_EMAIL', 'Not configured')}"
        )

        # Get current config from ConfigManager (single source of truth)
        config_yml = self.config_manager.get_full_config()

        # Show transcription from config.yml
        stt_default = config_yml.get("defaults", {}).get("stt", "not set")
        stt_model = next(
            (m for m in config_yml.get("models", []) if m.get("name") == stt_default),
            None,
        )
        stt_provider = (
            stt_model.get("model_provider", "unknown")
            if stt_model
            else "not configured"
        )
        self.console.print(
            f"✅ Transcription: {stt_provider} ({stt_default}) - config.yml"
        )

        # Show LLM config from config.yml
        llm_default = config_yml.get("defaults", {}).get("llm", "not set")
        embedding_default = config_yml.get("defaults", {}).get("embedding", "not set")
        self.console.print(f"✅ LLM: {llm_default} (config.yml)")
        self.console.print(f"✅ Embedding: {embedding_default} (config.yml)")

        # Show memory provider from config.yml
        memory_provider = config_yml.get("memory", {}).get("provider", "chronicle")
        self.console.print(f"✅ Memory Provider: {memory_provider} (config.yml)")

        # Show Obsidian/Neo4j status (read from config.yml)
        obsidian_config = config_yml.get("memory", {}).get("obsidian", {})
        if obsidian_config.get("enabled", False):
            neo4j_host = obsidian_config.get("neo4j_host", "not set")
            self.console.print(f"✅ Obsidian/Neo4j: Enabled ({neo4j_host})")

        # Show Knowledge Graph status (read from config.yml)
        kg_config = config_yml.get("memory", {}).get("knowledge_graph", {})
        if kg_config.get("enabled", False):
            neo4j_host = kg_config.get("neo4j_host", "not set")
            self.console.print(f"✅ Knowledge Graph: Enabled ({neo4j_host})")

        # Auto-determine URLs based on HTTPS configuration
        if self.config.get("HTTPS_ENABLED") == "true":
            server_ip = self.config.get("SERVER_IP", "localhost")
            self.console.print(f"✅ Backend URL: https://{server_ip}/")
            self.console.print(f"✅ Dashboard URL: https://{server_ip}/")
        else:
            backend_port = self.config.get("BACKEND_PUBLIC_PORT", "8000")
            webui_port = self.config.get("WEBUI_PORT", "5173")
            self.console.print(f"✅ Backend URL: http://localhost:{backend_port}")
            self.console.print(f"✅ Dashboard URL: http://localhost:{webui_port}")

    def show_next_steps(self):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        # Get current config from ConfigManager (single source of truth)
        config_yml = self.config_manager.get_full_config()

        self.console.print("1. Start the main services:")
        self.console.print("   [cyan]docker compose up --build -d[/cyan]")
        self.console.print()

        # Auto-determine URLs for next steps
        if self.config.get("HTTPS_ENABLED") == "true":
            server_ip = self.config.get("SERVER_IP", "localhost")
            self.console.print("2. Access the dashboard:")
            self.console.print(f"   [cyan]https://{server_ip}/[/cyan]")
            self.console.print()
            self.console.print("3. Check service health:")
            self.console.print(f"   [cyan]curl -k https://{server_ip}/health[/cyan]")
        else:
            webui_port = self.config.get("WEBUI_PORT", "5173")
            backend_port = self.config.get("BACKEND_PUBLIC_PORT", "8000")
            self.console.print("2. Access the dashboard:")
            self.console.print(f"   [cyan]http://localhost:{webui_port}[/cyan]")
            self.console.print()
            self.console.print("3. Check service health:")
            self.console.print(
                f"   [cyan]curl http://localhost:{backend_port}/health[/cyan]"
            )

        if self.config.get("MEMORY_PROVIDER") == "openmemory_mcp":
            self.console.print()
            self.console.print("4. Start OpenMemory MCP:")
            self.console.print(
                "   [cyan]cd ../../extras/openmemory-mcp && docker compose up -d[/cyan]"
            )

        if self.config.get("TRANSCRIPTION_PROVIDER") == "offline":
            self.console.print()
            self.console.print("5. Start Parakeet ASR:")
            self.console.print(
                "   [cyan]cd ../../extras/asr-services && docker compose up parakeet -d[/cyan]"
            )

    def run(self):
        """Run the complete setup process"""
        self.print_header("🚀 Chronicle Interactive Setup")
        self.console.print(
            "This wizard will help you configure Chronicle with all necessary services."
        )
        self.console.print(
            "[dim]Safe to run again — it backs up your config and preserves previous values.[/dim]"
        )
        self.console.print(
            "[dim]When unsure, just press Enter — the defaults will work.[/dim]"
        )
        self.console.print()

        try:
            # Backup existing config
            self.backup_existing_env()

            # Run setup steps
            self.setup_authentication()
            self.setup_transcription()
            self.setup_streaming_provider()
            self.setup_llm()
            self.setup_memory()
            self.setup_optional_services()
            self.setup_neo4j()
            self.setup_obsidian()
            self.setup_knowledge_graph()
            self.setup_langfuse()
            self.setup_network()
            self.setup_https()

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()
            self.copy_config_templates()

            # Show results
            self.show_summary()
            self.show_next_steps()

            self.console.print()
            self.console.print("[green][SUCCESS][/green] Setup complete! 🎉")
            self.console.print()
            self.console.print("📝 [bold]Configuration files updated:[/bold]")
            self.console.print(f"  • .env - API keys and environment variables")
            self.console.print(
                f"  • ../../config/config.yml - Model and memory provider configuration"
            )
            self.console.print()
            self.console.print("For detailed documentation, see:")
            self.console.print("  • Docs/quickstart.md")
            self.console.print("  • MEMORY_PROVIDERS.md")
            self.console.print("  • CLAUDE.md")

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Chronicle Advanced Backend Setup")
    parser.add_argument(
        "--speaker-service-url",
        help="Speaker Recognition service URL (default: prompt user)",
    )
    parser.add_argument(
        "--parakeet-asr-url", help="Parakeet ASR service URL (default: prompt user)"
    )
    parser.add_argument(
        "--transcription-provider",
        choices=["deepgram", "parakeet", "vibevoice", "qwen3-asr", "smallest", "none"],
        help="Transcription provider (default: prompt user)",
    )
    parser.add_argument(
        "--enable-https",
        action="store_true",
        help="Enable HTTPS configuration (default: prompt user)",
    )
    parser.add_argument(
        "--server-ip",
        help="Server IP/domain for SSL certificate (default: prompt user)",
    )
    parser.add_argument(
        "--enable-obsidian",
        action="store_true",
        help="Enable Obsidian/Neo4j integration (default: prompt user)",
    )
    parser.add_argument(
        "--enable-knowledge-graph",
        action="store_true",
        help="Enable Knowledge Graph entity extraction (default: prompt user)",
    )
    parser.add_argument(
        "--neo4j-password", help="Neo4j password (default: prompt user)"
    )
    parser.add_argument(
        "--ts-authkey",
        help="Tailscale auth key for Docker integration (default: prompt user)",
    )
    parser.add_argument(
        "--langfuse-public-key",
        help="LangFuse project public key (from langfuse init or external)",
    )
    parser.add_argument(
        "--langfuse-secret-key",
        help="LangFuse project secret key (from langfuse init or external)",
    )
    parser.add_argument(
        "--langfuse-host",
        help="LangFuse host URL (default: http://langfuse-web:3000 for local)",
    )
    parser.add_argument(
        "--langfuse-public-url",
        help="LangFuse browser-accessible URL for deep-links (default: http://localhost:3002)",
    )
    parser.add_argument(
        "--streaming-provider",
        choices=["deepgram", "smallest", "qwen3-asr"],
        help="Streaming provider when different from batch (enables batch re-transcription)",
    )

    args = parser.parse_args()

    setup = ChronicleSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
