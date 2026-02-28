#!/usr/bin/env python3
"""
Chronicle Root Setup Orchestrator
Handles service selection and delegation only - no configuration duplication
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from config_manager import ConfigManager
from rich.console import Console
from rich.prompt import Confirm, Prompt

# Import shared setup utilities
from setup_utils import (
    detect_tailscale_info,
    generate_self_signed_certs,
    generate_tailscale_certs,
    is_placeholder,
    mask_value,
    prompt_password,
    prompt_with_existing_masked,
    read_env_value,
)

console = Console()


def get_existing_stt_provider(config_yml: dict):
    """Map config.yml defaults.stt value back to wizard provider name, or None."""
    stt = config_yml.get("defaults", {}).get("stt", "")
    mapping = {
        "stt-deepgram": "deepgram",
        "stt-deepgram-stream": "deepgram",
        "stt-parakeet-batch": "parakeet",
        "stt-vibevoice": "vibevoice",
        "stt-qwen3-asr": "qwen3-asr",
        "stt-smallest": "smallest",
        "stt-smallest-stream": "smallest",
    }
    return mapping.get(stt)


def get_existing_stream_provider(config_yml: dict):
    """Map config.yml defaults.stt_stream value back to wizard streaming provider name, or None."""
    stt_stream = config_yml.get("defaults", {}).get("stt_stream", "")
    mapping = {
        "stt-deepgram-stream": "deepgram",
        "stt-smallest-stream": "smallest",
        "stt-qwen3-asr": "qwen3-asr",
        "stt-qwen3-asr-stream": "qwen3-asr",
    }
    return mapping.get(stt_stream)


SERVICES = {
    "backend": {
        "advanced": {
            "path": "backends/advanced",
            "cmd": [
                "uv",
                "run",
                "--with-requirements",
                "../../setup-requirements.txt",
                "python",
                "init.py",
            ],
            "description": "Advanced AI backend with full feature set",
            "required": True,
        }
    },
    "extras": {
        "speaker-recognition": {
            "path": "extras/speaker-recognition",
            "cmd": [
                "uv",
                "run",
                "--with-requirements",
                "../../setup-requirements.txt",
                "python",
                "init.py",
            ],
            "description": "Speaker identification and enrollment",
        },
        "asr-services": {
            "path": "extras/asr-services",
            "cmd": [
                "uv",
                "run",
                "--with-requirements",
                "../../setup-requirements.txt",
                "python",
                "init.py",
            ],
            "description": "Offline speech-to-text",
        },
        "openmemory-mcp": {
            "path": "extras/openmemory-mcp",
            "cmd": ["./setup.sh"],
            "description": "OpenMemory MCP server",
        },
        "langfuse": {
            "path": "extras/langfuse",
            "cmd": [
                "uv",
                "run",
                "--with-requirements",
                "../../setup-requirements.txt",
                "python",
                "init.py",
            ],
            "description": "LLM observability and prompt management (local)",
        },
    },
}


def discover_available_plugins():
    """
    Discover plugins by scanning plugins directory.

    Returns:
        Dictionary mapping plugin_id to plugin metadata:
        {
            'plugin_id': {
                'has_setup': bool,
                'setup_path': Path or None,
                'dir': Path
            }
        }
    """
    plugins_dir = Path("backends/advanced/src/advanced_omi_backend/plugins")

    if not plugins_dir.exists():
        console.print(
            f"[yellow]Warning: Plugins directory not found: {plugins_dir}[/yellow]"
        )
        return {}

    discovered = {}
    skip_dirs = {"__pycache__", "__init__.py", "base.py", "router.py"}

    for plugin_dir in plugins_dir.iterdir():
        if not plugin_dir.is_dir() or plugin_dir.name in skip_dirs:
            continue

        plugin_id = plugin_dir.name
        setup_script = plugin_dir / "setup.py"

        discovered[plugin_id] = {
            "has_setup": setup_script.exists(),
            "setup_path": setup_script if setup_script.exists() else None,
            "dir": plugin_dir,
        }

    return discovered


def check_service_exists(service_name, service_config):
    """Check if service directory and script exist"""
    service_path = Path(service_config["path"])
    if not service_path.exists():
        return False, f"Directory {service_path} does not exist"

    # For services with Python init scripts, check if init.py exists
    if service_name in ["advanced", "speaker-recognition", "asr-services", "langfuse"]:
        script_path = service_path / "init.py"
        if not script_path.exists():
            return False, f"Script {script_path} does not exist"
    else:
        # For other extras, check if setup.sh exists
        script_path = service_path / "setup.sh"
        if not script_path.exists():
            return (
                False,
                f"Script {script_path} does not exist (will be created in Phase 2)",
            )

    return True, "OK"


def select_services(transcription_provider=None, config_yml=None, memory_provider=None):
    """Let user select which services to setup"""
    config_yml = config_yml or {}
    console.print("🚀 [bold cyan]Chronicle Service Setup[/bold cyan]")
    console.print("Select which services to configure:\n")

    selected = []

    # Backend is required
    console.print("📱 [bold]Backend (Required):[/bold]")
    console.print("  ✅ Advanced Backend - Full AI features")
    selected.append("advanced")

    # Services that will be auto-added based on transcription provider choice
    auto_added = set()
    if transcription_provider in ("parakeet", "vibevoice", "qwen3-asr"):
        auto_added.add("asr-services")

    # Optional extras
    console.print("\n🔧 [bold]Optional Services:[/bold]")
    for service_name, service_config in SERVICES["extras"].items():
        # Skip services that will be auto-added based on earlier choices
        if service_name in auto_added:
            provider_label = {
                "vibevoice": "VibeVoice",
                "parakeet": "Parakeet",
                "qwen3-asr": "Qwen3-ASR",
            }.get(transcription_provider, transcription_provider)
            console.print(
                f"  ✅ {service_config['description']} ({provider_label}) [dim](auto-selected)[/dim]"
            )
            continue

        # LangFuse is handled separately via setup_langfuse_choice()
        if service_name == "langfuse":
            continue

        # Check if service exists
        exists, msg = check_service_exists(service_name, service_config)
        if not exists:
            console.print(f"  ⏸️  {service_config['description']} - [dim]{msg}[/dim]")
            continue

        # Determine smart default based on existing config
        if service_name == "speaker-recognition":
            # Default to True if speaker-recognition .env exists and has a valid (non-placeholder) HF_TOKEN
            speaker_env = "extras/speaker-recognition/.env"
            existing_hf = read_env_value(speaker_env, "HF_TOKEN")
            default_enable = bool(
                existing_hf
                and not is_placeholder(
                    existing_hf,
                    "your_huggingface_token_here",
                    "your-huggingface-token-here",
                    "hf_xxxxx",
                )
            )
        elif service_name == "openmemory-mcp":
            # Default to True if memory provider was selected as openmemory_mcp
            default_enable = memory_provider == "openmemory_mcp"
        else:
            default_enable = False

        try:
            enable_service = Confirm.ask(
                f"  Setup {service_config['description']}?", default=default_enable
            )
        except EOFError:
            console.print(f"Using default: {'Yes' if default_enable else 'No'}")
            enable_service = default_enable

        if enable_service:
            selected.append(service_name)

    return selected


def cleanup_unselected_services(selected_services):
    """Backup and remove .env files from services that weren't selected"""

    all_services = list(SERVICES["backend"].keys()) + list(SERVICES["extras"].keys())

    for service_name in all_services:
        if service_name not in selected_services:
            if service_name == "advanced":
                service_path = Path(SERVICES["backend"][service_name]["path"])
            else:
                service_path = Path(SERVICES["extras"][service_name]["path"])

            env_file = service_path / ".env"
            if env_file.exists():
                # Create backup with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_file = service_path / f".env.backup.{timestamp}.unselected"
                env_file.rename(backup_file)
                console.print(
                    f"🧹 [dim]Backed up {service_name} configuration to {backup_file.name} (service not selected)[/dim]"
                )


def run_service_setup(
    service_name,
    selected_services,
    https_enabled=False,
    server_ip=None,
    obsidian_enabled=False,
    neo4j_password=None,
    hf_token=None,
    transcription_provider="deepgram",
    admin_email=None,
    admin_password=None,
    langfuse_public_key=None,
    langfuse_secret_key=None,
    langfuse_host=None,
    streaming_provider=None,
    llm_provider=None,
    memory_provider=None,
    knowledge_graph_enabled=None,
    hardware_profile=None,
):
    """Execute individual service setup script"""
    if service_name == "advanced":
        service = SERVICES["backend"][service_name]

        # For advanced backend, pass URLs of other selected services and HTTPS config
        cmd = service["cmd"].copy()
        if "speaker-recognition" in selected_services:
            cmd.extend(["--speaker-service-url", "http://speaker-service:8085"])
        if "asr-services" in selected_services:
            cmd.extend(["--parakeet-asr-url", "host.docker.internal:8767"])

        # Pass transcription provider choice from wizard
        if transcription_provider:
            cmd.extend(["--transcription-provider", transcription_provider])

        # Pass streaming provider (different from batch) for re-transcription setup
        if streaming_provider:
            cmd.extend(["--streaming-provider", streaming_provider])

        # Add HTTPS configuration
        if https_enabled and server_ip:
            cmd.extend(["--enable-https", "--server-ip", server_ip])

        # Always pass Neo4j password (neo4j is a required service)
        if neo4j_password:
            cmd.extend(["--neo4j-password", neo4j_password])

        # Always pass obsidian choice to avoid double-ask
        if obsidian_enabled:
            cmd.extend(["--enable-obsidian"])
        else:
            cmd.extend(["--no-obsidian"])

        # Always pass knowledge graph choice to avoid double-ask
        if knowledge_graph_enabled is True:
            cmd.extend(["--enable-knowledge-graph"])
        elif knowledge_graph_enabled is False:
            cmd.extend(["--no-knowledge-graph"])

        # Pass LLM provider choice
        if llm_provider:
            cmd.extend(["--llm-provider", llm_provider])

        # Pass memory provider choice
        if memory_provider:
            cmd.extend(["--memory-provider", memory_provider])

        # Pass LangFuse keys from langfuse init or external config
        if langfuse_public_key and langfuse_secret_key:
            cmd.extend(["--langfuse-public-key", langfuse_public_key])
            cmd.extend(["--langfuse-secret-key", langfuse_secret_key])
            if langfuse_host:
                cmd.extend(["--langfuse-host", langfuse_host])

    else:
        service = SERVICES["extras"][service_name]
        cmd = service["cmd"].copy()

        # Add HTTPS configuration for services that support it
        if service_name == "speaker-recognition" and https_enabled and server_ip:
            cmd.extend(["--enable-https", "--server-ip", server_ip])

        # For speaker-recognition, pass HF_TOKEN from centralized configuration
        if service_name == "speaker-recognition":
            # Define the speaker env path
            speaker_env_path = "extras/speaker-recognition/.env"

            # Pass explicit hardware profile selection when provided by wizard
            if hardware_profile == "strixhalo":
                cmd.extend(["--pytorch-cuda-version", "strixhalo"])
                cmd.extend(["--compute-mode", "gpu"])
                console.print(
                    "[blue][INFO][/blue] Using AMD Strix Halo profile for speaker recognition"
                )

            # HF Token should have been provided via setup_hf_token_if_needed()
            if hf_token:
                cmd.extend(["--hf-token", hf_token])
            else:
                console.print(
                    "[yellow][WARNING][/yellow] No HF_TOKEN provided - speaker recognition may fail to download models"
                )

            # Pass Deepgram API key from backend if available
            backend_env_path = "backends/advanced/.env"
            deepgram_key = read_env_value(backend_env_path, "DEEPGRAM_API_KEY")
            if deepgram_key and not is_placeholder(
                deepgram_key, "your_deepgram_api_key_here", "your-deepgram-api-key-here"
            ):
                cmd.extend(["--deepgram-api-key", deepgram_key])
                console.print(
                    "[blue][INFO][/blue] Found existing DEEPGRAM_API_KEY from backend config, reusing"
                )

            # Pass compute mode from existing .env if available
            compute_mode = read_env_value(speaker_env_path, "COMPUTE_MODE")
            if hardware_profile != "strixhalo" and compute_mode in ["cpu", "gpu"]:
                cmd.extend(["--compute-mode", compute_mode])
                console.print(
                    f"[blue][INFO][/blue] Found existing COMPUTE_MODE ({compute_mode}), reusing"
                )

        # For asr-services, pass provider from wizard's transcription choice and reuse CUDA version
        if service_name == "asr-services":
            # Map wizard transcription provider to asr-services provider name
            if hardware_profile == "strixhalo":
                wizard_to_asr_provider = {
                    "vibevoice": "vibevoice-strixhalo",
                    "parakeet": "nemo-strixhalo",
                    "qwen3-asr": "qwen3-asr",
                }
            else:
                wizard_to_asr_provider = {
                    "vibevoice": "vibevoice",
                    "parakeet": "nemo",
                    "qwen3-asr": "qwen3-asr",
                }
            asr_provider = wizard_to_asr_provider.get(transcription_provider)
            if asr_provider:
                cmd.extend(["--provider", asr_provider])
                console.print(
                    f"[blue][INFO][/blue] Pre-selecting ASR provider: {asr_provider} (from wizard choice: {transcription_provider})"
                )

            speaker_env_path = "extras/speaker-recognition/.env"
            cuda_version = read_env_value(speaker_env_path, "PYTORCH_CUDA_VERSION")
            if hardware_profile == "strixhalo":
                cmd.extend(["--pytorch-cuda-version", "strixhalo"])
                console.print(
                    "[blue][INFO][/blue] Using AMD Strix Halo profile for ASR services"
                )
            elif cuda_version and cuda_version in [
                "cu121",
                "cu126",
                "cu128",
                "strixhalo",
            ]:
                cmd.extend(["--pytorch-cuda-version", cuda_version])
                console.print(
                    f"[blue][INFO][/blue] Found existing PYTORCH_CUDA_VERSION ({cuda_version}) from speaker-recognition, reusing"
                )

        # For langfuse, pass admin credentials from backend
        if service_name == "langfuse":
            if admin_email:
                cmd.extend(["--admin-email", admin_email])
            if admin_password:
                cmd.extend(["--admin-password", admin_password])

        # For openmemory-mcp, try to pass OpenAI API key from backend if available
        if service_name == "openmemory-mcp":
            backend_env_path = "backends/advanced/.env"
            openmemory_env_path = "extras/openmemory-mcp/.env"
            openai_key = read_env_value(backend_env_path, "OPENAI_API_KEY")
            backend_openai_base_url = read_env_value(
                backend_env_path, "OPENAI_BASE_URL"
            )
            backend_embedding_model = read_env_value(
                backend_env_path, "OPENAI_EMBEDDING_MODEL"
            )
            backend_embedding_dims = read_env_value(
                backend_env_path, "OPENAI_EMBEDDING_DIMENSIONS"
            )

            existing_embeddings_provider = read_env_value(
                openmemory_env_path, "OPENMEMORY_EMBEDDINGS_PROVIDER"
            )
            existing_embeddings_base_url = read_env_value(
                openmemory_env_path, "OPENMEMORY_EMBEDDINGS_BASE_URL"
            )
            existing_embeddings_model = read_env_value(
                openmemory_env_path, "OPENMEMORY_EMBEDDINGS_MODEL"
            )
            existing_embeddings_api_key = read_env_value(
                openmemory_env_path, "OPENMEMORY_EMBEDDINGS_API_KEY"
            )
            existing_embeddings_dims = read_env_value(
                openmemory_env_path, "OPENMEMORY_EMBEDDINGS_DIMENSIONS"
            )

            def _has_value(value):
                return value and value.strip()

            has_openai_key = _has_value(openai_key) and not is_placeholder(
                openai_key,
                "your_openai_api_key_here",
                "your-openai-api-key-here",
                "your_openai_key_here",
                "your-openai-key-here",
            )

            # Prefer an existing OpenMemory local embedding configuration if available.
            if (
                existing_embeddings_provider == "local"
                and _has_value(existing_embeddings_base_url)
                and _has_value(existing_embeddings_model)
                and _has_value(existing_embeddings_api_key)
                and _has_value(existing_embeddings_dims)
            ):
                cmd.extend(["--embeddings-provider", "local"])
                cmd.extend(["--embeddings-base-url", existing_embeddings_base_url])
                cmd.extend(["--embeddings-model", existing_embeddings_model])
                cmd.extend(["--embeddings-api-key", existing_embeddings_api_key])
                cmd.extend(["--embeddings-dimensions", existing_embeddings_dims])
                console.print(
                    "[blue][INFO][/blue] Found existing local embeddings config for OpenMemory, reusing"
                )
            elif (
                has_openai_key
                and _has_value(backend_openai_base_url)
                and "api.openai.com" not in backend_openai_base_url
            ):
                # Backend appears to use a local OpenAI-compatible endpoint.
                cmd.extend(["--embeddings-provider", "local"])
                cmd.extend(["--embeddings-base-url", backend_openai_base_url])
                cmd.extend(["--embeddings-api-key", openai_key])
                if _has_value(backend_embedding_model):
                    cmd.extend(["--embeddings-model", backend_embedding_model])
                if _has_value(backend_embedding_dims):
                    cmd.extend(["--embeddings-dimensions", backend_embedding_dims])
                console.print(
                    "[blue][INFO][/blue] Found OpenAI-compatible local endpoint in backend config, pre-filling OpenMemory local embeddings"
                )
            elif has_openai_key:
                cmd.extend(["--openai-api-key", openai_key])
                console.print(
                    "[blue][INFO][/blue] Found existing OPENAI_API_KEY from backend config, reusing"
                )

    console.print(f"\n🔧 [bold]Setting up {service_name}...[/bold]")

    # Check if service exists before running
    exists, msg = check_service_exists(service_name, service)
    if not exists:
        console.print(f"❌ {service_name} setup failed: {msg}")
        return False

    try:
        result = subprocess.run(
            cmd,
            cwd=service["path"],
            check=True,
            timeout=300,  # 5 minute timeout for service setup
        )

        console.print(f"✅ {service_name} setup completed")
        return True

    except FileNotFoundError as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        console.print(
            f"[yellow]   Check that the service directory exists: {service['path']}[/yellow]"
        )
        console.print(
            f"[yellow]   And that 'uv' is installed and on your PATH[/yellow]"
        )
        return False
    except subprocess.TimeoutExpired as e:
        console.print(f"❌ {service_name} setup timed out after {e.timeout}s")
        console.print(f"[yellow]   Configuration may be partially written.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(
            f"[yellow]   cd {service['path']} && {' '.join(service['cmd'])}[/yellow]"
        )
        return False
    except subprocess.CalledProcessError as e:
        console.print(f"❌ {service_name} setup failed with exit code {e.returncode}")
        console.print(f"[yellow]   Check the error output above for details.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(
            f"[yellow]   cd {service['path']} && {' '.join(service['cmd'])}[/yellow]"
        )
        return False
    except Exception as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        return False


def show_service_status():
    """Show which services are available"""
    console.print("\n📋 [bold]Service Status:[/bold]")

    # Check backend
    exists, msg = check_service_exists("advanced", SERVICES["backend"]["advanced"])
    status = "✅" if exists else "❌"
    console.print(f"  {status} Advanced Backend - {msg}")

    # Check extras
    for service_name, service_config in SERVICES["extras"].items():
        exists, msg = check_service_exists(service_name, service_config)
        status = "✅" if exists else "⏸️"
        console.print(f"  {status} {service_config['description']} - {msg}")


def run_plugin_setup(plugin_id, plugin_info):
    """Run a plugin's setup.py script"""
    setup_path = plugin_info["setup_path"]

    try:
        # Run plugin setup script interactively (don't capture output)
        # This allows the plugin to prompt for user input
        result = subprocess.run(
            [
                "uv",
                "run",
                "--with-requirements",
                "setup-requirements.txt",
                "python",
                str(setup_path),
            ],
            cwd=str(Path.cwd()),
        )

        if result.returncode == 0:
            console.print(f"\n[green]✅ {plugin_id} configured successfully[/green]")
            return True
        else:
            console.print(
                f"\n[red]❌ {plugin_id} setup failed with exit code {result.returncode}[/red]"
            )
            return False

    except Exception as e:
        console.print(f"[red]❌ Error running {plugin_id} setup: {e}[/red]")
        return False


def setup_plugins():
    """Discover and setup plugins via delegation"""
    console.print("\n🔌 [bold cyan]Plugin Configuration[/bold cyan]")
    console.print("Chronicle supports community plugins for extended functionality.\n")

    # Discover available plugins
    available_plugins = discover_available_plugins()

    if not available_plugins:
        console.print("[dim]No plugins found[/dim]")
        return

    # Ask about enabling community plugins
    try:
        enable_plugins = Confirm.ask("Enable community plugins?", default=True)
    except EOFError:
        console.print("Using default: Yes")
        enable_plugins = True

    if not enable_plugins:
        console.print("[dim]Skipping plugin configuration[/dim]")
        return

    # For each plugin with setup script
    configured_count = 0
    for plugin_id, plugin_info in available_plugins.items():
        if not plugin_info["has_setup"]:
            console.print(
                f"[dim]  {plugin_id}: No setup wizard available (configure manually)[/dim]"
            )
            continue

        # Ask if user wants to configure this plugin
        try:
            configure = Confirm.ask(f"  Configure {plugin_id} plugin?", default=False)
        except EOFError:
            configure = False

        if configure:
            # Delegate to plugin's setup script
            console.print(f"\n[cyan]Running {plugin_id} setup wizard...[/cyan]")
            success = run_plugin_setup(plugin_id, plugin_info)
            if success:
                configured_count += 1

    console.print(f"\n[green]✅ Configured {configured_count} plugin(s)[/green]")


def setup_git_hooks():
    """Setup pre-commit hooks for development"""
    console.print("\n🔧 [bold]Setting up development environment...[/bold]")

    # Check if git is available
    if not shutil.which("git"):
        console.print(
            "⚠️  [yellow]git not found, skipping git hooks setup (optional)[/yellow]"
        )
        return

    try:
        # Install pre-commit via uv tool (uv is our package manager)
        subprocess.run(
            ["uv", "tool", "install", "pre-commit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        # Install git hooks
        result = subprocess.run(
            ["pre-commit", "install", "--hook-type", "pre-push"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            console.print(
                "✅ [green]Git hooks installed (tests will run before push)[/green]"
            )
        else:
            console.print("⚠️  [yellow]Could not install git hooks (optional)[/yellow]")

        # Also install pre-commit hook
        subprocess.run(
            ["pre-commit", "install", "--hook-type", "pre-commit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    except Exception as e:
        console.print(f"⚠️  [yellow]Could not setup git hooks: {e} (optional)[/yellow]")


def setup_hf_token_if_needed(selected_services):
    """Prompt for Hugging Face token if needed by selected services.

    Args:
        selected_services: List of service names selected by user

    Returns:
        HF_TOKEN string if provided, None otherwise
    """
    # Check if any selected services need HF_TOKEN
    needs_hf_token = "speaker-recognition" in selected_services

    if not needs_hf_token:
        return None

    console.print("\n🤗 [bold cyan]Hugging Face Token Configuration[/bold cyan]")
    console.print("Required for speaker recognition (PyAnnote models)")
    console.print(
        "\n[blue][INFO][/blue] Get your token from: https://huggingface.co/settings/tokens"
    )
    console.print()
    console.print(
        "[yellow]⚠️  You must also accept the model agreements for these gated models:[/yellow]"
    )
    console.print("   1. [cyan]Speaker Diarization[/cyan]")
    console.print(
        "      https://huggingface.co/pyannote/speaker-diarization-community-1"
    )
    console.print("   2. [cyan]Segmentation Model[/cyan]")
    console.print("      https://huggingface.co/pyannote/segmentation-3.0")
    console.print("   3. [cyan]Segmentation Model[/cyan]")
    console.print("      https://huggingface.co/pyannote/segmentation-3.1")
    console.print("   4. [cyan]Embedding Model[/cyan]")
    console.print(
        "      https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM"
    )
    console.print()
    console.print(
        "[yellow]→[/yellow] Open each link and click 'Agree and access repository'"
    )
    console.print("[yellow]→[/yellow] Use the same Hugging Face account as your token")
    console.print()

    # Check for existing token from speaker-recognition service
    speaker_env_path = "extras/speaker-recognition/.env"
    existing_token = read_env_value(speaker_env_path, "HF_TOKEN")

    # Use the masked prompt function
    hf_token = prompt_with_existing_masked(
        prompt_text="Hugging Face Token",
        existing_value=existing_token,
        placeholders=[
            "your_huggingface_token_here",
            "your-huggingface-token-here",
            "hf_xxxxx",
        ],
        is_password=True,
        default="",
    )

    if hf_token:
        masked = mask_value(hf_token)
        console.print(f"[green]✅ HF_TOKEN configured: {masked}[/green]\n")
        return hf_token
    else:
        console.print(
            "[yellow]⚠️  No HF_TOKEN provided - speaker recognition may fail[/yellow]\n"
        )
        return None


# Providers that support real-time streaming
STREAMING_CAPABLE = {"deepgram", "smallest", "qwen3-asr"}


def select_transcription_provider(config_yml: dict = None):
    """Ask user which transcription provider they want (batch/primary)."""
    config_yml = config_yml or {}
    existing_provider = get_existing_stt_provider(config_yml)

    provider_to_choice = {
        "deepgram": "1",
        "parakeet": "2",
        "vibevoice": "3",
        "qwen3-asr": "4",
        "smallest": "5",
        "none": "6",
    }
    choice_to_provider = {v: k for k, v in provider_to_choice.items()}
    default_choice = provider_to_choice.get(existing_provider, "1")

    console.print("\n🎤 [bold cyan]Transcription Provider[/bold cyan]")
    console.print(
        "Choose your speech-to-text provider (used for [bold]batch[/bold]/high-quality transcription):"
    )
    console.print(
        "[dim]If it also supports streaming, it will be used for real-time too by default.[/dim]"
    )
    if existing_provider:
        provider_labels = {
            "deepgram": "Deepgram",
            "parakeet": "Parakeet ASR",
            "vibevoice": "VibeVoice ASR",
            "qwen3-asr": "Qwen3-ASR",
            "smallest": "Smallest.ai Pulse",
        }
        console.print(
            f"[blue][INFO][/blue] Current: {provider_labels.get(existing_provider, existing_provider)}"
        )
    console.print()

    choices = {
        "1": "Deepgram (cloud, streaming + batch)",
        "2": "Parakeet ASR (offline, batch only, GPU)",
        "3": "VibeVoice ASR (offline, batch only, built-in diarization, GPU)",
        "4": "Qwen3-ASR (offline, streaming + batch, 52 languages, GPU)",
        "5": "Smallest.ai Pulse (cloud, streaming + batch)",
        "6": "None (skip transcription setup)",
    }

    for key, desc in choices.items():
        marker = " [dim](current)[/dim]" if key == default_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
            if choice in choices:
                return choice_to_provider[choice]
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            console.print(f"Using default: {choices.get(default_choice, 'Deepgram')}")
            return choice_to_provider.get(default_choice, "deepgram")


def select_streaming_provider(batch_provider, config_yml: dict = None):
    """Ask if user wants a different provider for real-time streaming.

    If the batch provider supports streaming, offer to use the same (saves a step).
    If it's batch-only, the user must pick a streaming provider or skip.

    Returns:
        Streaming provider name if different from batch, or None (same / skipped).
    """
    config_yml = config_yml or {}
    if batch_provider in ("none", None):
        return None

    existing_stream = get_existing_stream_provider(config_yml)

    if batch_provider in STREAMING_CAPABLE:
        # Batch provider can already stream — just confirm
        # Default to "use different" if a different streaming provider was previously configured
        has_different_stream = bool(
            existing_stream and existing_stream != batch_provider
        )
        console.print(f"\n🔊 [bold cyan]Streaming[/bold cyan]")
        console.print(f"{batch_provider} supports both batch and streaming.")
        try:
            use_different = Confirm.ask(
                "Use a different provider for real-time streaming?",
                default=has_different_stream,
            )
        except EOFError:
            return None
        if not use_different:
            return None
    else:
        # Batch-only provider — need to pick a streaming provider
        console.print(f"\n🔊 [bold cyan]Streaming[/bold cyan]")
        console.print(
            f"{batch_provider} is batch-only. Pick a streaming provider for real-time transcription:"
        )

    # Show streaming-capable providers (excluding the batch provider)
    streaming_choices = {}
    provider_map = {}
    idx = 1

    for name, desc in [
        ("deepgram", "Deepgram (cloud, streaming)"),
        ("smallest", "Smallest.ai Pulse (cloud, streaming)"),
        ("qwen3-asr", "Qwen3-ASR (offline, streaming)"),
    ]:
        if name != batch_provider:
            streaming_choices[str(idx)] = desc
            provider_map[str(idx)] = name
            idx += 1

    skip_key = str(idx)
    streaming_choices[skip_key] = "Skip (no real-time streaming)"
    provider_map[skip_key] = None

    # Pre-select the default based on existing config
    default_stream_choice = "1"
    if existing_stream and existing_stream != batch_provider:
        for k, v in provider_map.items():
            if v == existing_stream:
                default_stream_choice = k
                break

    for key, desc in streaming_choices.items():
        marker = " [dim](current)[/dim]" if key == default_stream_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_stream_choice)
            if choice in streaming_choices:
                result = provider_map[choice]
                if result:
                    console.print(
                        f"[green]✅[/green] Streaming: {result}, Batch: {batch_provider}"
                    )
                return result
            console.print(
                f"[red]Invalid choice. Please select from {list(streaming_choices.keys())}[/red]"
            )
        except EOFError:
            return None


def setup_langfuse_choice():
    """Ask user about LangFuse configuration: local or external.

    LangFuse is always enabled (required for prompt management and observability).
    The only choice is whether to use the bundled local instance or an existing external one.

    Returns:
        Tuple of (mode, config) where:
        - mode: 'local' or 'external'
        - config: dict with keys {host, public_key, secret_key} for external, empty for local
    """
    console.print("\n📊 [bold cyan]LangFuse Configuration[/bold cyan]")
    console.print("LangFuse provides LLM observability, tracing, and prompt management")
    console.print()

    try:
        has_existing = Confirm.ask(
            "Use an existing external LangFuse instance instead of local?",
            default=False,
        )
    except EOFError:
        console.print("Using default: No (will set up locally)")
        has_existing = False

    if not has_existing:
        # Check if the local langfuse directory exists
        exists, msg = check_service_exists("langfuse", SERVICES["extras"]["langfuse"])
        if exists:
            console.print("[green]✅[/green] Will set up local LangFuse instance")
            return "local", {}
        else:
            console.print(f"[yellow]⚠️  Local LangFuse not available: {msg}[/yellow]")
            console.print(
                "[yellow]   Will proceed without LangFuse — add it later when available[/yellow]"
            )
            return "local", {}

    # External LangFuse — collect connection details
    console.print()
    console.print("[bold]Enter your external LangFuse connection details:[/bold]")

    backend_env_path = "backends/advanced/.env"

    existing_host = read_env_value(backend_env_path, "LANGFUSE_HOST")
    # Don't treat the local docker host as an existing external value
    if existing_host and "langfuse-web" in existing_host:
        existing_host = None

    host = prompt_with_existing_masked(
        prompt_text="LangFuse host URL",
        existing_value=existing_host,
        placeholders=[""],
        is_password=False,
        default="https://cloud.langfuse.com",
    )

    existing_pub = read_env_value(backend_env_path, "LANGFUSE_PUBLIC_KEY")
    public_key = prompt_with_existing_masked(
        prompt_text="LangFuse public key",
        existing_value=existing_pub,
        placeholders=[""],
        is_password=False,
        default="",
    )

    existing_sec = read_env_value(backend_env_path, "LANGFUSE_SECRET_KEY")
    secret_key = prompt_with_existing_masked(
        prompt_text="LangFuse secret key",
        existing_value=existing_sec,
        placeholders=[""],
        is_password=True,
        default="",
    )

    if not (host and public_key and secret_key):
        console.print(
            "[yellow]⚠️  Incomplete LangFuse configuration — skipping[/yellow]"
        )
        return None, {}

    console.print(f"[green]✅[/green] External LangFuse configured: {host}")
    return "external", {
        "host": host,
        "public_key": public_key,
        "secret_key": secret_key,
    }


def select_hardware_profile(
    selected_services, transcription_provider, streaming_provider
):
    """Select hardware profile for GPU-backed optional services.

    Returns:
        "strixhalo" for AMD Strix Halo profile, otherwise None.
    """
    strix_capable_providers = {"parakeet", "vibevoice"}
    needs_hardware_choice = (
        "speaker-recognition" in selected_services
        or transcription_provider in strix_capable_providers
        or streaming_provider in strix_capable_providers
    )

    if not needs_hardware_choice:
        return None

    console.print("\n🧠 [bold cyan]Hardware Profile[/bold cyan]")
    console.print(
        "Choose target hardware for GPU services (speaker recognition and offline ASR):"
    )
    choices = {
        "1": "Standard (CPU/NVIDIA CUDA)",
        "2": "AMD Strix Halo (ROCm, gfx1151 / Ryzen AI Max)",
    }
    for key, desc in choices.items():
        console.print(f"  {key}) {desc}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default="1")
            if choice == "1":
                return None
            if choice == "2":
                console.print(
                    "[green]✅[/green] Using AMD Strix Halo profile where supported"
                )
                return "strixhalo"
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            return None


def select_llm_provider(config_yml: dict = None) -> str:
    """Ask user which LLM provider to use for memory extraction.

    Returns:
        "openai", "ollama", or "none"
    """
    config_yml = config_yml or {}
    existing_llm = config_yml.get("defaults", {}).get("llm", "")
    llm_to_choice = {"openai-llm": "1", "local-llm": "2"}
    default_choice = llm_to_choice.get(existing_llm, "1")

    console.print("\n🤖 [bold cyan]LLM Provider[/bold cyan]")
    console.print(
        "Choose your language model provider for memory extraction and analysis:"
    )
    console.print()

    choices = {
        "1": "OpenAI (GPT-4o-mini, requires API key)",
        "2": "Ollama (local models, runs on your machine)",
        "3": "None (skip memory extraction)",
    }

    for key, desc in choices.items():
        marker = " [dim](current)[/dim]" if key == default_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
            if choice in choices:
                return {"1": "openai", "2": "ollama", "3": "none"}[choice]
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            console.print(f"Using default: {choices.get(default_choice, 'OpenAI')}")
            return {"1": "openai", "2": "ollama", "3": "none"}.get(
                default_choice, "openai"
            )


def select_memory_provider(config_yml: dict = None) -> str:
    """Ask user which memory storage backend to use.

    This is separate from the 'Setup OpenMemory MCP server?' service question.
    That question is about running the extra service; this is about the backend provider.

    Returns:
        "chronicle" or "openmemory_mcp"
    """
    config_yml = config_yml or {}
    existing_provider = config_yml.get("memory", {}).get("provider", "chronicle")
    default_choice = "2" if existing_provider == "openmemory_mcp" else "1"

    console.print("\n🧠 [bold cyan]Memory Storage Backend[/bold cyan]")
    console.print("Choose where your memories and conversation facts are stored:")
    console.print()

    choices = {
        "1": "Chronicle Native (Qdrant vector database, self-hosted)",
        "2": "OpenMemory MCP (cross-client compatible, requires openmemory-mcp service)",
    }

    for key, desc in choices.items():
        marker = " [dim](current)[/dim]" if key == default_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
            if choice in choices:
                return {"1": "chronicle", "2": "openmemory_mcp"}[choice]
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            return {"1": "chronicle", "2": "openmemory_mcp"}.get(
                default_choice, "chronicle"
            )


def select_knowledge_graph(config_yml: dict = None) -> bool:
    """Ask user if Knowledge Graph should be enabled.

    Returns:
        True if Knowledge Graph should be enabled, False otherwise.
    """
    config_yml = config_yml or {}
    existing_enabled = (
        config_yml.get("memory", {}).get("knowledge_graph", {}).get("enabled", True)
    )

    console.print("\n🕸️  [bold cyan]Knowledge Graph[/bold cyan]")
    console.print(
        "Extracts people, places, organizations, events, and tasks from conversations"
    )
    console.print("Uses Neo4j (included in the stack)")
    console.print()

    try:
        enabled = Confirm.ask("Enable Knowledge Graph?", default=existing_enabled)
    except EOFError:
        console.print(f"Using default: {'Yes' if existing_enabled else 'No'}")
        enabled = existing_enabled

    return enabled


def main():
    """Main orchestration logic"""
    console.print("🎉 [bold green]Welcome to Chronicle![/bold green]\n")
    console.print("[dim]This wizard is safe to run as many times as you like.[/dim]")
    console.print(
        "[dim]It backs up your existing config and preserves previously entered values.[/dim]"
    )
    console.print(
        "[dim]When unsure, just press Enter — the defaults will work.[/dim]\n"
    )

    # Ensure config.yml exists (create from template if needed)
    config_mgr = ConfigManager()
    config_mgr.ensure_config_yml()

    # Setup git hooks first
    setup_git_hooks()

    # Show what's available
    show_service_status()

    # Read existing config.yml once — used as defaults for ALL wizard questions below
    config_yml = config_mgr.get_full_config()

    # Ask about transcription provider FIRST (determines which services are needed)
    transcription_provider = select_transcription_provider(config_yml)

    # Ask about streaming provider (if batch provider doesn't stream, or user wants a different one)
    streaming_provider = select_streaming_provider(transcription_provider, config_yml)

    # LLM Provider selection (asked once here, passed to init.py — avoids double-ask)
    llm_provider = select_llm_provider(config_yml)

    # Memory Provider selection (asked once here, passed to init.py — avoids double-ask)
    memory_provider = select_memory_provider(config_yml)

    # Service Selection (pass transcription_provider so we skip asking about ASR when already chosen)
    selected_services = select_services(
        transcription_provider, config_yml, memory_provider
    )

    # Auto-add asr-services if any local ASR was chosen (batch or streaming)
    local_asr_providers = ("parakeet", "vibevoice", "qwen3-asr")
    needs_asr = transcription_provider in local_asr_providers or (
        streaming_provider and streaming_provider in local_asr_providers
    )
    if needs_asr and "asr-services" not in selected_services:
        reason = (
            transcription_provider
            if transcription_provider in local_asr_providers
            else streaming_provider
        )
        console.print(
            f"[blue][INFO][/blue] Auto-adding ASR services for {reason} transcription"
        )
        selected_services.append("asr-services")

    # Auto-add openmemory-mcp service if openmemory_mcp was selected as memory provider
    if (
        memory_provider == "openmemory_mcp"
        and "openmemory-mcp" not in selected_services
    ):
        exists, _ = check_service_exists(
            "openmemory-mcp", SERVICES["extras"]["openmemory-mcp"]
        )
        if exists:
            console.print(
                "[blue][INFO][/blue] Memory provider is OpenMemory MCP — auto-adding openmemory-mcp service"
            )
            selected_services.append("openmemory-mcp")

    if not selected_services:
        console.print("\n[yellow]No services selected. Exiting.[/yellow]")
        return

    # LangFuse Configuration (before service setup so keys can be passed to backend)
    langfuse_mode, langfuse_external = setup_langfuse_choice()
    if langfuse_mode == "local" and "langfuse" not in selected_services:
        selected_services.append("langfuse")

    # HF Token Configuration (if services require it)
    hardware_profile = select_hardware_profile(
        selected_services, transcription_provider, streaming_provider
    )

    hf_token = setup_hf_token_if_needed(selected_services)

    # HTTPS Configuration (for services that need it)
    https_enabled = False
    server_ip = None

    # Check if we have services that benefit from HTTPS
    https_services = {
        "advanced",
        "speaker-recognition",
    }  # advanced will always need https then
    needs_https = bool(https_services.intersection(selected_services))

    if needs_https:
        console.print("\n🔒 [bold cyan]HTTPS Configuration[/bold cyan]")
        console.print(
            "HTTPS enables microphone access in browsers and secure connections"
        )

        # Default to existing HTTPS_ENABLED setting
        existing_https = read_env_value("backends/advanced/.env", "HTTPS_ENABLED")
        default_https = existing_https == "true"

        try:
            https_enabled = Confirm.ask(
                "Enable HTTPS for selected services?", default=default_https
            )
        except EOFError:
            console.print(f"Using default: {'Yes' if default_https else 'No'}")
            https_enabled = default_https

        if https_enabled:
            # Try to auto-detect Tailscale address
            ts_dns, ts_ip = detect_tailscale_info()

            if ts_dns:
                console.print(
                    f"\n[green][AUTO-DETECTED][/green] Tailscale DNS: {ts_dns}"
                )
                if ts_ip:
                    console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale IP:  {ts_ip}"
                    )
                default_address = ts_dns
            elif ts_ip:
                console.print(f"\n[green][AUTO-DETECTED][/green] Tailscale IP: {ts_ip}")
                default_address = ts_ip
            else:
                console.print("\n[blue][INFO][/blue] Tailscale not detected")
                console.print(
                    "[blue][INFO][/blue] To find your Tailscale address: tailscale status --json | jq -r '.Self.DNSName'"
                )
                default_address = None

            console.print("[blue][INFO][/blue] For local-only access, use 'localhost'")
            console.print("Examples: localhost, myhost.tail1234.ts.net, 100.64.1.2")

            # Check for existing SERVER_IP from backend .env
            backend_env_path = "backends/advanced/.env"
            existing_ip = read_env_value(backend_env_path, "SERVER_IP")

            # Use existing value, or auto-detected address, or localhost as default
            effective_default = default_address or "localhost"

            server_ip = prompt_with_existing_masked(
                prompt_text="Server IP/Domain for SSL certificates",
                existing_value=existing_ip,
                placeholders=["localhost", "your-server-ip-here"],
                is_password=False,
                default=effective_default,
            )

            console.print(f"[green]✅[/green] HTTPS configured for: {server_ip}")

            # Generate certificates centrally in certs/
            console.print("\n[blue][INFO][/blue] Generating TLS certificates...")
            if ts_dns and generate_tailscale_certs("certs"):
                console.print(
                    f"[green]✅[/green] Tailscale trusted certs generated in certs/ for {ts_dns}"
                )
            elif generate_self_signed_certs(server_ip, "certs"):
                console.print(
                    f"[green]✅[/green] Self-signed certs generated in certs/ for {server_ip}"
                )
            else:
                console.print(
                    "[yellow]⚠️  Certificate generation failed. "
                    "Generate manually: cd certs && ./generate-ssl.sh <address>[/yellow]"
                )

    # Neo4j Configuration (always required - used by Knowledge Graph)
    neo4j_password = None
    obsidian_enabled = False
    knowledge_graph_enabled = None

    if "advanced" in selected_services:
        console.print("\n🗄️ [bold cyan]Neo4j Configuration[/bold cyan]")
        console.print(
            "Neo4j is used for Knowledge Graph (entity/relationship extraction from conversations)"
        )
        console.print()

        # Read existing Neo4j password and use as default (masked prompt)
        existing_neo4j_pw = read_env_value("backends/advanced/.env", "NEO4J_PASSWORD")
        neo4j_password = prompt_with_existing_masked(
            prompt_text="Neo4j password (min 8 chars)",
            existing_value=existing_neo4j_pw,
            placeholders=[
                "neo4jpassword",
                "your_neo4j_password",
                "your-neo4j-password",
            ],
            is_password=True,
            default="neo4jpassword",
        )
        if not neo4j_password:
            neo4j_password = "neo4jpassword"

        console.print("[green]✅[/green] Neo4j configured")

        # Obsidian is optional (graph-based knowledge management for vault notes)
        console.print("\n🗂️ [bold cyan]Obsidian Integration (Optional)[/bold cyan]")
        console.print(
            "Enable graph-based knowledge management for Obsidian vault notes"
        )
        console.print()

        # Load existing obsidian enabled state from config.yml as default
        existing_obsidian = (
            config_yml.get("memory", {}).get("obsidian", {}).get("enabled", False)
        )
        try:
            obsidian_enabled = Confirm.ask(
                "Enable Obsidian integration?", default=existing_obsidian
            )
        except EOFError:
            console.print(f"Using default: {'Yes' if existing_obsidian else 'No'}")
            obsidian_enabled = existing_obsidian

        if obsidian_enabled:
            console.print("[green]✅[/green] Obsidian integration will be configured")

        # Knowledge Graph configuration (asked here once, passed to init.py)
        knowledge_graph_enabled = select_knowledge_graph(config_yml)

    # Pure Delegation - Run Each Service Setup
    console.print(f"\n📋 [bold]Setting up {len(selected_services)} services...[/bold]")

    # Clean up .env files from unselected services (creates backups)
    cleanup_unselected_services(selected_services)

    success_count = 0
    failed_services = []

    # Pre-populate langfuse keys from external config (if user chose external mode)
    langfuse_public_key = langfuse_external.get("public_key")
    langfuse_secret_key = langfuse_external.get("secret_key")
    langfuse_host = langfuse_external.get(
        "host"
    )  # None for local (backend defaults to langfuse-web)

    # Determine setup order: langfuse first (to get API keys), then backend (with langfuse keys), then others
    setup_order = []
    if "langfuse" in selected_services:
        setup_order.append("langfuse")
    if "advanced" in selected_services:
        setup_order.append("advanced")
    for service in selected_services:
        if service not in setup_order:
            setup_order.append(service)

    # Read admin credentials from existing backend .env (for langfuse init reuse)
    backend_env_path = "backends/advanced/.env"
    wizard_admin_email = read_env_value(backend_env_path, "ADMIN_EMAIL")
    wizard_admin_password = read_env_value(backend_env_path, "ADMIN_PASSWORD")

    for service in setup_order:
        if run_service_setup(
            service,
            selected_services,
            https_enabled,
            server_ip,
            obsidian_enabled,
            neo4j_password,
            hf_token,
            transcription_provider,
            admin_email=wizard_admin_email,
            admin_password=wizard_admin_password,
            langfuse_public_key=langfuse_public_key,
            langfuse_secret_key=langfuse_secret_key,
            langfuse_host=langfuse_host,
            streaming_provider=streaming_provider,
            llm_provider=llm_provider,
            memory_provider=memory_provider,
            knowledge_graph_enabled=knowledge_graph_enabled,
            hardware_profile=hardware_profile,
        ):
            success_count += 1

            # After local langfuse setup, read generated API keys for backend
            if service == "langfuse":
                langfuse_env_path = "extras/langfuse/.env"
                langfuse_public_key = read_env_value(
                    langfuse_env_path, "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"
                )
                langfuse_secret_key = read_env_value(
                    langfuse_env_path, "LANGFUSE_INIT_PROJECT_SECRET_KEY"
                )
                if langfuse_public_key and langfuse_secret_key:
                    console.print(
                        "[blue][INFO][/blue] LangFuse API keys will be passed to backend configuration"
                    )
        else:
            failed_services.append(service)

    # Plugin Configuration (AFTER backend .env is created)
    # This ensures plugins can add their secrets to the existing .env file
    # without the backend init overwriting them
    setup_plugins()

    # Final Summary
    console.print(f"\n🎊 [bold green]Setup Complete![/bold green]")
    console.print(
        f"✅ {success_count}/{len(selected_services)} services configured successfully"
    )

    if failed_services:
        console.print(f"❌ Failed services: {', '.join(failed_services)}")

    # Next Steps
    console.print("\n📖 [bold]Next Steps:[/bold]")

    # Configuration info
    console.print("")
    console.print("📝 [bold cyan]Configuration Files Updated:[/bold cyan]")
    console.print("   • [green].env files[/green] - API keys and service URLs")
    console.print(
        "   • [green]config.yml[/green] - Model definitions and memory provider settings"
    )
    console.print("")

    # Development Environment Setup
    console.print("1. Setup development environment (git hooks, testing):")
    console.print("   [cyan]make setup-dev[/cyan]")
    console.print(
        "   [dim]This installs pre-commit hooks to run tests before pushing[/dim]"
    )
    console.print("")

    # Service Management Commands
    console.print("2. Start all configured services:")
    console.print("   [cyan]./start.sh[/cyan]")
    console.print(
        "   [dim]Or: uv run --with-requirements setup-requirements.txt python services.py start --all --build[/dim]"
    )
    console.print("")
    console.print("3. Or start individual services:")

    configured_services = []
    if "advanced" in selected_services and "advanced" not in failed_services:
        configured_services.append("backend")
    if (
        "speaker-recognition" in selected_services
        and "speaker-recognition" not in failed_services
    ):
        configured_services.append("speaker-recognition")
    if "asr-services" in selected_services and "asr-services" not in failed_services:
        configured_services.append("asr-services")
    if (
        "openmemory-mcp" in selected_services
        and "openmemory-mcp" not in failed_services
    ):
        configured_services.append("openmemory-mcp")
    if "langfuse" in selected_services and "langfuse" not in failed_services:
        configured_services.append("langfuse")

    # LangFuse prompt management info
    if langfuse_mode == "local" and "langfuse" not in failed_services:
        console.print("")
        console.print(
            "[bold cyan]Prompt Management:[/bold cyan] Once services are running, edit AI prompts at:"
        )
        if https_enabled and server_ip:
            console.print(
                f"   [link=https://{server_ip}:3443/project/chronicle/prompts]https://{server_ip}:3443/project/chronicle/prompts[/link]"
            )
        else:
            console.print(
                "   [link=http://localhost:3002/project/chronicle/prompts]http://localhost:3002/project/chronicle/prompts[/link]"
            )
    elif langfuse_mode == "external" and langfuse_host:
        console.print("")
        console.print(
            f"[bold cyan]Prompt Management:[/bold cyan] Edit AI prompts at your LangFuse instance:"
        )
        console.print(f"   {langfuse_host}")

    if configured_services:
        service_list = " ".join(configured_services)
        console.print(
            f"   [cyan]uv run --with-requirements setup-requirements.txt python services.py start {service_list}[/cyan]"
        )

    console.print("")
    console.print("3. Check service status:")
    console.print("   [cyan]./status.sh[/cyan]")
    console.print(
        "   [dim]Or: uv run --with-requirements setup-requirements.txt python services.py status[/dim]"
    )

    console.print("")
    console.print("4. Stop services when done:")
    console.print("   [cyan]./stop.sh[/cyan]")
    console.print(
        "   [dim]Or: uv run --with-requirements setup-requirements.txt python services.py stop --all[/dim]"
    )

    console.print(f"\n🚀 [bold]Enjoy Chronicle![/bold]")

    # Show individual service usage
    console.print(f"\n💡 [dim]Tip: You can also setup services individually:[/dim]")
    console.print(
        f"[dim]   cd backends/advanced && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]"
    )
    console.print(
        f"[dim]   cd extras/speaker-recognition && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]"
    )
    console.print(
        f"[dim]   cd extras/asr-services && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]"
    )


if __name__ == "__main__":
    main()
