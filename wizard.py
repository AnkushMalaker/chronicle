#!/usr/bin/env python3
"""
Chronicle Root Setup Orchestrator
Handles service selection and delegation only - no configuration duplication
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

# Import shared setup utilities
from setup_utils import (
    detect_tailscale_info,
    is_placeholder,
    mask_value,
    prompt_password,
    prompt_with_existing_masked,
    read_env_value,
)

console = Console()

SERVICES = {
    'backend': {
        'advanced': {
            'path': 'backends/advanced',
            'cmd': ['uv', 'run', '--with-requirements', '../../setup-requirements.txt', 'python', 'init.py'],
            'description': 'Advanced AI backend with full feature set',
            'required': True
        }
    },
    'extras': {
        'speaker-recognition': {
            'path': 'extras/speaker-recognition',
            'cmd': ['uv', 'run', '--with-requirements', '../../setup-requirements.txt', 'python', 'init.py'],
            'description': 'Speaker identification and enrollment'
        },
        'asr-services': {
            'path': 'extras/asr-services',
            'cmd': ['uv', 'run', '--with-requirements', '../../setup-requirements.txt', 'python', 'init.py'],
            'description': 'Offline speech-to-text'
        },
        'openmemory-mcp': {
            'path': 'extras/openmemory-mcp',
            'cmd': ['./setup.sh'],
            'description': 'OpenMemory MCP server'
        },
        'langfuse': {
            'path': 'extras/langfuse',
            'cmd': ['uv', 'run', '--with-requirements', '../../setup-requirements.txt', 'python', 'init.py'],
            'description': 'LLM observability and prompt management (local)'
        }
    }
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
        console.print(f"[yellow]Warning: Plugins directory not found: {plugins_dir}[/yellow]")
        return {}

    discovered = {}
    skip_dirs = {'__pycache__', '__init__.py', 'base.py', 'router.py'}

    for plugin_dir in plugins_dir.iterdir():
        if not plugin_dir.is_dir() or plugin_dir.name in skip_dirs:
            continue

        plugin_id = plugin_dir.name
        setup_script = plugin_dir / "setup.py"

        discovered[plugin_id] = {
            'has_setup': setup_script.exists(),
            'setup_path': setup_script if setup_script.exists() else None,
            'dir': plugin_dir
        }

    return discovered

def check_service_exists(service_name, service_config):
    """Check if service directory and script exist"""
    service_path = Path(service_config['path'])
    if not service_path.exists():
        return False, f"Directory {service_path} does not exist"

    # For services with Python init scripts, check if init.py exists
    if service_name in ['advanced', 'speaker-recognition', 'asr-services', 'langfuse']:
        script_path = service_path / 'init.py'
        if not script_path.exists():
            return False, f"Script {script_path} does not exist"
    else:
        # For other extras, check if setup.sh exists
        script_path = service_path / 'setup.sh'
        if not script_path.exists():
            return False, f"Script {script_path} does not exist (will be created in Phase 2)"

    return True, "OK"

def select_services(transcription_provider=None):
    """Let user select which services to setup"""
    console.print("🚀 [bold cyan]Chronicle Service Setup[/bold cyan]")
    console.print("Select which services to configure:\n")

    selected = []

    # Backend is required
    console.print("📱 [bold]Backend (Required):[/bold]")
    console.print("  ✅ Advanced Backend - Full AI features")
    selected.append('advanced')

    # Services that will be auto-added based on transcription provider choice
    auto_added = set()
    if transcription_provider in ("parakeet", "vibevoice"):
        auto_added.add('asr-services')

    # Optional extras
    console.print("\n🔧 [bold]Optional Services:[/bold]")
    for service_name, service_config in SERVICES['extras'].items():
        # Skip services that will be auto-added based on earlier choices
        if service_name in auto_added:
            provider_label = {"vibevoice": "VibeVoice", "parakeet": "Parakeet"}.get(transcription_provider, transcription_provider)
            console.print(f"  ✅ {service_config['description']} ({provider_label}) [dim](auto-selected)[/dim]")

        # LangFuse is handled separately via setup_langfuse_choice()
        if service_name == 'langfuse':
            continue

        # Check if service exists
        exists, msg = check_service_exists(service_name, service_config)
        if not exists:
            console.print(f"  ⏸️  {service_config['description']} - [dim]{msg}[/dim]")
            continue

        # Speaker recognition is recommended by default
        default_enable = service_name == 'speaker-recognition'

        try:
            enable_service = Confirm.ask(f"  Setup {service_config['description']}?", default=default_enable)
        except EOFError:
            console.print(f"Using default: {'Yes' if default_enable else 'No'}")
            enable_service = default_enable

        if enable_service:
            selected.append(service_name)

    return selected

def cleanup_unselected_services(selected_services):
    """Backup and remove .env files from services that weren't selected"""
    
    all_services = list(SERVICES['backend'].keys()) + list(SERVICES['extras'].keys())
    
    for service_name in all_services:
        if service_name not in selected_services:
            if service_name == 'advanced':
                service_path = Path(SERVICES['backend'][service_name]['path'])
            else:
                service_path = Path(SERVICES['extras'][service_name]['path'])
            
            env_file = service_path / '.env'
            if env_file.exists():
                # Create backup with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_file = service_path / f'.env.backup.{timestamp}.unselected'
                env_file.rename(backup_file)
                console.print(f"🧹 [dim]Backed up {service_name} configuration to {backup_file.name} (service not selected)[/dim]")

def run_service_setup(service_name, selected_services, https_enabled=False, server_ip=None,
                     obsidian_enabled=False, neo4j_password=None, hf_token=None,
                     transcription_provider='deepgram', admin_email=None, admin_password=None,
                     langfuse_public_key=None, langfuse_secret_key=None, langfuse_host=None):
    """Execute individual service setup script"""
    if service_name == 'advanced':
        service = SERVICES['backend'][service_name]

        # For advanced backend, pass URLs of other selected services and HTTPS config
        cmd = service['cmd'].copy()
        if 'speaker-recognition' in selected_services:
            cmd.extend(['--speaker-service-url', 'http://speaker-service:8085'])
        if 'asr-services' in selected_services:
            cmd.extend(['--parakeet-asr-url', 'http://host.docker.internal:8767'])

        # Pass transcription provider choice from wizard
        if transcription_provider:
            cmd.extend(['--transcription-provider', transcription_provider])

        # Add HTTPS configuration
        if https_enabled and server_ip:
            cmd.extend(['--enable-https', '--server-ip', server_ip])

        # Always pass Neo4j password (neo4j is a required service)
        if neo4j_password:
            cmd.extend(['--neo4j-password', neo4j_password])

        # Add Obsidian configuration
        if obsidian_enabled:
            cmd.extend(['--enable-obsidian'])

        # Pass LangFuse keys from langfuse init or external config
        if langfuse_public_key and langfuse_secret_key:
            cmd.extend(['--langfuse-public-key', langfuse_public_key])
            cmd.extend(['--langfuse-secret-key', langfuse_secret_key])
            if langfuse_host:
                cmd.extend(['--langfuse-host', langfuse_host])

    else:
        service = SERVICES['extras'][service_name]
        cmd = service['cmd'].copy()
        
        # Add HTTPS configuration for services that support it
        if service_name == 'speaker-recognition' and https_enabled and server_ip:
            cmd.extend(['--enable-https', '--server-ip', server_ip])

        # For speaker-recognition, pass HF_TOKEN from centralized configuration
        if service_name == 'speaker-recognition':
            # Define the speaker env path
            speaker_env_path = 'extras/speaker-recognition/.env'

            # HF Token should have been provided via setup_hf_token_if_needed()
            if hf_token:
                cmd.extend(['--hf-token', hf_token])
            else:
                console.print("[yellow][WARNING][/yellow] No HF_TOKEN provided - speaker recognition may fail to download models")

            # Pass Deepgram API key from backend if available
            backend_env_path = 'backends/advanced/.env'
            deepgram_key = read_env_value(backend_env_path, 'DEEPGRAM_API_KEY')
            if deepgram_key and not is_placeholder(deepgram_key, 'your_deepgram_api_key_here', 'your-deepgram-api-key-here'):
                cmd.extend(['--deepgram-api-key', deepgram_key])
                console.print("[blue][INFO][/blue] Found existing DEEPGRAM_API_KEY from backend config, reusing")

            # Pass compute mode from existing .env if available
            compute_mode = read_env_value(speaker_env_path, 'COMPUTE_MODE')
            if compute_mode in ['cpu', 'gpu']:
                cmd.extend(['--compute-mode', compute_mode])
                console.print(f"[blue][INFO][/blue] Found existing COMPUTE_MODE ({compute_mode}), reusing")
        
        # For asr-services, pass provider from wizard's transcription choice and reuse CUDA version
        if service_name == 'asr-services':
            # Map wizard transcription provider to asr-services provider name
            wizard_to_asr_provider = {
                'vibevoice': 'vibevoice',
                'parakeet': 'nemo',
            }
            asr_provider = wizard_to_asr_provider.get(transcription_provider)
            if asr_provider:
                cmd.extend(['--provider', asr_provider])
                console.print(f"[blue][INFO][/blue] Pre-selecting ASR provider: {asr_provider} (from wizard choice: {transcription_provider})")

            speaker_env_path = 'extras/speaker-recognition/.env'
            cuda_version = read_env_value(speaker_env_path, 'PYTORCH_CUDA_VERSION')
            if cuda_version and cuda_version in ['cu121', 'cu126', 'cu128']:
                cmd.extend(['--pytorch-cuda-version', cuda_version])
                console.print(f"[blue][INFO][/blue] Found existing PYTORCH_CUDA_VERSION ({cuda_version}) from speaker-recognition, reusing")

        # For langfuse, pass admin credentials from backend
        if service_name == 'langfuse':
            if admin_email:
                cmd.extend(['--admin-email', admin_email])
            if admin_password:
                cmd.extend(['--admin-password', admin_password])

        # For openmemory-mcp, try to pass OpenAI API key from backend if available
        if service_name == 'openmemory-mcp':
            backend_env_path = 'backends/advanced/.env'
            openai_key = read_env_value(backend_env_path, 'OPENAI_API_KEY')
            if openai_key and not is_placeholder(openai_key, 'your_openai_api_key_here', 'your-openai-api-key-here', 'your_openai_key_here', 'your-openai-key-here'):
                cmd.extend(['--openai-api-key', openai_key])
                console.print("[blue][INFO][/blue] Found existing OPENAI_API_KEY from backend config, reusing")
    
    console.print(f"\n🔧 [bold]Setting up {service_name}...[/bold]")
    
    # Check if service exists before running
    exists, msg = check_service_exists(service_name, service)
    if not exists:
        console.print(f"❌ {service_name} setup failed: {msg}")
        return False
    
    try:
        result = subprocess.run(
            cmd, 
            cwd=service['path'],
            check=True,
            timeout=300  # 5 minute timeout for service setup
        )
        
        console.print(f"✅ {service_name} setup completed")
        return True
            
    except FileNotFoundError as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        console.print(f"[yellow]   Check that the service directory exists: {service['path']}[/yellow]")
        console.print(f"[yellow]   And that 'uv' is installed and on your PATH[/yellow]")
        return False
    except subprocess.TimeoutExpired as e:
        console.print(f"❌ {service_name} setup timed out after {e.timeout}s")
        console.print(f"[yellow]   Configuration may be partially written.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(f"[yellow]   cd {service['path']} && {' '.join(service['cmd'])}[/yellow]")
        return False
    except subprocess.CalledProcessError as e:
        console.print(f"❌ {service_name} setup failed with exit code {e.returncode}")
        console.print(f"[yellow]   Check the error output above for details.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(f"[yellow]   cd {service['path']} && {' '.join(service['cmd'])}[/yellow]")
        return False
    except Exception as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        return False

def show_service_status():
    """Show which services are available"""
    console.print("\n📋 [bold]Service Status:[/bold]")
    
    # Check backend
    exists, msg = check_service_exists('advanced', SERVICES['backend']['advanced'])
    status = "✅" if exists else "❌"
    console.print(f"  {status} Advanced Backend - {msg}")
    
    # Check extras
    for service_name, service_config in SERVICES['extras'].items():
        exists, msg = check_service_exists(service_name, service_config)
        status = "✅" if exists else "⏸️"
        console.print(f"  {status} {service_config['description']} - {msg}")

def run_plugin_setup(plugin_id, plugin_info):
    """Run a plugin's setup.py script"""
    setup_path = plugin_info['setup_path']

    try:
        # Run plugin setup script interactively (don't capture output)
        # This allows the plugin to prompt for user input
        result = subprocess.run(
            ['uv', 'run', '--with-requirements', 'setup-requirements.txt', 'python', str(setup_path)],
            cwd=str(Path.cwd())
        )

        if result.returncode == 0:
            console.print(f"\n[green]✅ {plugin_id} configured successfully[/green]")
            return True
        else:
            console.print(f"\n[red]❌ {plugin_id} setup failed with exit code {result.returncode}[/red]")
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
        enable_plugins = Confirm.ask(
            "Enable community plugins?",
            default=True
        )
    except EOFError:
        console.print("Using default: Yes")
        enable_plugins = True

    if not enable_plugins:
        console.print("[dim]Skipping plugin configuration[/dim]")
        return

    # For each plugin with setup script
    configured_count = 0
    for plugin_id, plugin_info in available_plugins.items():
        if not plugin_info['has_setup']:
            console.print(f"[dim]  {plugin_id}: No setup wizard available (configure manually)[/dim]")
            continue

        # Ask if user wants to configure this plugin
        try:
            configure = Confirm.ask(
                f"  Configure {plugin_id} plugin?",
                default=False
            )
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

    try:
        # Install pre-commit if not already installed
        subprocess.run(['pip', 'install', 'pre-commit'],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL,
                      check=False)

        # Install git hooks
        result = subprocess.run(['pre-commit', 'install', '--hook-type', 'pre-push'],
                              capture_output=True,
                              text=True)

        if result.returncode == 0:
            console.print("✅ [green]Git hooks installed (tests will run before push)[/green]")
        else:
            console.print("⚠️  [yellow]Could not install git hooks (optional)[/yellow]")

        # Also install pre-commit hook
        subprocess.run(['pre-commit', 'install', '--hook-type', 'pre-commit'],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL,
                      check=False)

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
    needs_hf_token = 'speaker-recognition' in selected_services

    if not needs_hf_token:
        return None

    console.print("\n🤗 [bold cyan]Hugging Face Token Configuration[/bold cyan]")
    console.print("Required for speaker recognition (PyAnnote models)")
    console.print("\n[blue][INFO][/blue] Get your token from: https://huggingface.co/settings/tokens")
    console.print()
    console.print("[yellow]⚠️  You must also accept the model agreements for these gated models:[/yellow]")
    console.print("   1. [cyan]Speaker Diarization[/cyan]")
    console.print("      https://huggingface.co/pyannote/speaker-diarization-community-1")
    console.print("   2. [cyan]Segmentation Model[/cyan]")
    console.print("      https://huggingface.co/pyannote/segmentation-3.0")
    console.print("   3. [cyan]Segmentation Model[/cyan]")
    console.print("      https://huggingface.co/pyannote/segmentation-3.1")
    console.print("   4. [cyan]Embedding Model[/cyan]")
    console.print("      https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM")
    console.print()
    console.print("[yellow]→[/yellow] Open each link and click 'Agree and access repository'")
    console.print("[yellow]→[/yellow] Use the same Hugging Face account as your token")
    console.print()

    # Check for existing token from speaker-recognition service
    speaker_env_path = 'extras/speaker-recognition/.env'
    existing_token = read_env_value(speaker_env_path, 'HF_TOKEN')

    # Use the masked prompt function
    hf_token = prompt_with_existing_masked(
        prompt_text="Hugging Face Token",
        existing_value=existing_token,
        placeholders=['your_huggingface_token_here', 'your-huggingface-token-here', 'hf_xxxxx'],
        is_password=True,
        default=""
    )

    if hf_token:
        masked = mask_value(hf_token)
        console.print(f"[green]✅ HF_TOKEN configured: {masked}[/green]\n")
        return hf_token
    else:
        console.print("[yellow]⚠️  No HF_TOKEN provided - speaker recognition may fail[/yellow]\n")
        return None

def setup_config_file():
    """Setup config/config.yml from template if it doesn't exist"""
    config_file = Path("config/config.yml")
    config_template = Path("config/config.yml.template")

    if not config_file.exists():
        if config_template.exists():
            # Ensure config/ directory exists
            config_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(config_template, config_file)
            console.print("✅ [green]Created config/config.yml from template[/green]")
        else:
            console.print("⚠️  [yellow]config/config.yml.template not found, skipping config setup[/yellow]")
    else:
        console.print("ℹ️  [blue]config/config.yml already exists, keeping existing configuration[/blue]")

def select_transcription_provider():
    """Ask user which transcription provider they want"""
    console.print("\n🎤 [bold cyan]Transcription Provider[/bold cyan]")
    console.print("Choose your speech-to-text provider:")
    console.print()

    choices = {
        "1": "Deepgram (cloud-based, high quality, requires API key)",
        "2": "Parakeet ASR (offline, runs locally, requires GPU)",
        "3": "VibeVoice ASR (offline, built-in speaker diarization, requires GPU)",
        "4": "None (skip transcription setup)"
    }

    for key, desc in choices.items():
        console.print(f"  {key}) {desc}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default="1")
            if choice in choices:
                if choice == "1":
                    return "deepgram"
                elif choice == "2":
                    return "parakeet"
                elif choice == "3":
                    return "vibevoice"
                elif choice == "4":
                    return "none"
            console.print(f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]")
        except EOFError:
            console.print("Using default: Deepgram")
            return "deepgram"

def setup_langfuse_choice():
    """Ask user about LangFuse configuration: local, external, or disabled.

    Returns:
        Tuple of (mode, config) where:
        - mode: 'local', 'external', or None (disabled)
        - config: dict with keys {host, public_key, secret_key} for external, empty for local/None
    """
    console.print("\n📊 [bold cyan]LangFuse Configuration[/bold cyan]")
    console.print("LangFuse provides LLM observability, tracing, and prompt management")
    console.print()

    try:
        enable = Confirm.ask("Enable LangFuse?", default=True)
    except EOFError:
        console.print("Using default: Yes")
        enable = True

    if not enable:
        console.print("[blue][INFO][/blue] LangFuse disabled")
        return None, {}

    try:
        has_existing = Confirm.ask("Do you have an existing LangFuse installation?", default=False)
    except EOFError:
        console.print("Using default: No (will set up locally)")
        has_existing = False

    if not has_existing:
        # Check if the local langfuse directory exists
        exists, msg = check_service_exists('langfuse', SERVICES['extras']['langfuse'])
        if exists:
            console.print("[green]✅[/green] Will set up local LangFuse instance")
            return 'local', {}
        else:
            console.print(f"[yellow]⚠️  Local LangFuse not available: {msg}[/yellow]")
            console.print("[yellow]   Skipping LangFuse setup[/yellow]")
            return None, {}

    # External LangFuse — collect connection details
    console.print()
    console.print("[bold]Enter your external LangFuse connection details:[/bold]")

    backend_env_path = 'backends/advanced/.env'

    existing_host = read_env_value(backend_env_path, 'LANGFUSE_HOST')
    # Don't treat the local docker host as an existing external value
    if existing_host and 'langfuse-web' in existing_host:
        existing_host = None

    host = prompt_with_existing_masked(
        prompt_text="LangFuse host URL",
        existing_value=existing_host,
        placeholders=[""],
        is_password=False,
        default="https://cloud.langfuse.com"
    )

    existing_pub = read_env_value(backend_env_path, 'LANGFUSE_PUBLIC_KEY')
    public_key = prompt_with_existing_masked(
        prompt_text="LangFuse public key",
        existing_value=existing_pub,
        placeholders=[""],
        is_password=False,
        default=""
    )

    existing_sec = read_env_value(backend_env_path, 'LANGFUSE_SECRET_KEY')
    secret_key = prompt_with_existing_masked(
        prompt_text="LangFuse secret key",
        existing_value=existing_sec,
        placeholders=[""],
        is_password=True,
        default=""
    )

    if not (host and public_key and secret_key):
        console.print("[yellow]⚠️  Incomplete LangFuse configuration — skipping[/yellow]")
        return None, {}

    console.print(f"[green]✅[/green] External LangFuse configured: {host}")
    return 'external', {
        'host': host,
        'public_key': public_key,
        'secret_key': secret_key,
    }


def main():
    """Main orchestration logic"""
    console.print("🎉 [bold green]Welcome to Chronicle![/bold green]\n")
    console.print("[dim]This wizard is safe to run as many times as you like.[/dim]")
    console.print("[dim]It backs up your existing config and preserves previously entered values.[/dim]")
    console.print("[dim]When unsure, just press Enter — the defaults will work.[/dim]\n")

    # Setup config file from template
    setup_config_file()

    # Setup git hooks first
    setup_git_hooks()

    # Show what's available
    show_service_status()

    # Ask about transcription provider FIRST (determines which services are needed)
    transcription_provider = select_transcription_provider()

    # Service Selection (pass transcription_provider so we skip asking about ASR when already chosen)
    selected_services = select_services(transcription_provider)

    # Auto-add asr-services if local ASR was chosen (Parakeet or VibeVoice)
    if transcription_provider in ("parakeet", "vibevoice") and 'asr-services' not in selected_services:
        console.print(f"[blue][INFO][/blue] Auto-adding ASR services for {transcription_provider.capitalize()} transcription")
        selected_services.append('asr-services')

    if not selected_services:
        console.print("\n[yellow]No services selected. Exiting.[/yellow]")
        return

    # LangFuse Configuration (before service setup so keys can be passed to backend)
    langfuse_mode, langfuse_external = setup_langfuse_choice()
    if langfuse_mode == 'local' and 'langfuse' not in selected_services:
        selected_services.append('langfuse')

    # HF Token Configuration (if services require it)
    hf_token = setup_hf_token_if_needed(selected_services)

    # HTTPS Configuration (for services that need it)
    https_enabled = False
    server_ip = None
    
    # Check if we have services that benefit from HTTPS
    https_services = {'advanced', 'speaker-recognition'} # advanced will always need https then
    needs_https = bool(https_services.intersection(selected_services))
    
    if needs_https:
        console.print("\n🔒 [bold cyan]HTTPS Configuration[/bold cyan]")
        console.print("HTTPS enables microphone access in browsers and secure connections")

        try:
            https_enabled = Confirm.ask("Enable HTTPS for selected services?", default=False)
        except EOFError:
            console.print("Using default: No")
            https_enabled = False

        if https_enabled:
            # Try to auto-detect Tailscale address
            ts_dns, ts_ip = detect_tailscale_info()

            if ts_dns:
                console.print(f"\n[green][AUTO-DETECTED][/green] Tailscale DNS: {ts_dns}")
                if ts_ip:
                    console.print(f"[green][AUTO-DETECTED][/green] Tailscale IP:  {ts_ip}")
                default_address = ts_dns
            elif ts_ip:
                console.print(f"\n[green][AUTO-DETECTED][/green] Tailscale IP: {ts_ip}")
                default_address = ts_ip
            else:
                console.print("\n[blue][INFO][/blue] Tailscale not detected")
                console.print("[blue][INFO][/blue] To find your Tailscale address: tailscale status --json | jq -r '.Self.DNSName'")
                default_address = None

            console.print("[blue][INFO][/blue] For local-only access, use 'localhost'")
            console.print("Examples: localhost, myhost.tail1234.ts.net, 100.64.1.2")

            # Check for existing SERVER_IP from backend .env
            backend_env_path = 'backends/advanced/.env'
            existing_ip = read_env_value(backend_env_path, 'SERVER_IP')

            # Use existing value, or auto-detected address, or localhost as default
            effective_default = default_address or "localhost"

            server_ip = prompt_with_existing_masked(
                prompt_text="Server IP/Domain for SSL certificates",
                existing_value=existing_ip,
                placeholders=['localhost', 'your-server-ip-here'],
                is_password=False,
                default=effective_default
            )

            console.print(f"[green]✅[/green] HTTPS configured for: {server_ip}")

    # Neo4j Configuration (always required - used by Knowledge Graph)
    neo4j_password = None
    obsidian_enabled = False

    if 'advanced' in selected_services:
        console.print("\n🗄️ [bold cyan]Neo4j Configuration[/bold cyan]")
        console.print("Neo4j is used for Knowledge Graph (entity/relationship extraction from conversations)")
        console.print()

        # Always prompt for Neo4j password (masked input)
        try:
            console.print("Neo4j password (min 8 chars) [leave empty for default: neo4jpassword]")
            neo4j_password = prompt_password("Neo4j password", min_length=8)
        except (EOFError, KeyboardInterrupt):
            neo4j_password = "neo4jpassword"
            console.print("Using default password")
        if not neo4j_password:
            neo4j_password = "neo4jpassword"

        console.print("[green]✅[/green] Neo4j configured")

        # Obsidian is optional (graph-based knowledge management for vault notes)
        console.print("\n🗂️ [bold cyan]Obsidian Integration (Optional)[/bold cyan]")
        console.print("Enable graph-based knowledge management for Obsidian vault notes")
        console.print()

        try:
            obsidian_enabled = Confirm.ask("Enable Obsidian integration?", default=False)
        except EOFError:
            console.print("Using default: No")
            obsidian_enabled = False

        if obsidian_enabled:
            console.print("[green]✅[/green] Obsidian integration will be configured")

    # Pure Delegation - Run Each Service Setup
    console.print(f"\n📋 [bold]Setting up {len(selected_services)} services...[/bold]")

    # Clean up .env files from unselected services (creates backups)
    cleanup_unselected_services(selected_services)

    success_count = 0
    failed_services = []

    # Pre-populate langfuse keys from external config (if user chose external mode)
    langfuse_public_key = langfuse_external.get('public_key')
    langfuse_secret_key = langfuse_external.get('secret_key')
    langfuse_host = langfuse_external.get('host')  # None for local (backend defaults to langfuse-web)

    # Determine setup order: langfuse first (to get API keys), then backend (with langfuse keys), then others
    setup_order = []
    if 'langfuse' in selected_services:
        setup_order.append('langfuse')
    if 'advanced' in selected_services:
        setup_order.append('advanced')
    for service in selected_services:
        if service not in setup_order:
            setup_order.append(service)

    # Read admin credentials from existing backend .env (for langfuse init reuse)
    backend_env_path = 'backends/advanced/.env'
    wizard_admin_email = read_env_value(backend_env_path, 'ADMIN_EMAIL')
    wizard_admin_password = read_env_value(backend_env_path, 'ADMIN_PASSWORD')

    for service in setup_order:
        if run_service_setup(service, selected_services, https_enabled, server_ip,
                            obsidian_enabled, neo4j_password, hf_token, transcription_provider,
                            admin_email=wizard_admin_email, admin_password=wizard_admin_password,
                            langfuse_public_key=langfuse_public_key, langfuse_secret_key=langfuse_secret_key,
                            langfuse_host=langfuse_host):
            success_count += 1

            # After local langfuse setup, read generated API keys for backend
            if service == 'langfuse':
                langfuse_env_path = 'extras/langfuse/.env'
                langfuse_public_key = read_env_value(langfuse_env_path, 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY')
                langfuse_secret_key = read_env_value(langfuse_env_path, 'LANGFUSE_INIT_PROJECT_SECRET_KEY')
                if langfuse_public_key and langfuse_secret_key:
                    console.print("[blue][INFO][/blue] LangFuse API keys will be passed to backend configuration")
        else:
            failed_services.append(service)

    # Plugin Configuration (AFTER backend .env is created)
    # This ensures plugins can add their secrets to the existing .env file
    # without the backend init overwriting them
    setup_plugins()

    # Final Summary
    console.print(f"\n🎊 [bold green]Setup Complete![/bold green]")
    console.print(f"✅ {success_count}/{len(selected_services)} services configured successfully")

    if failed_services:
        console.print(f"❌ Failed services: {', '.join(failed_services)}")
    
    # Next Steps
    console.print("\n📖 [bold]Next Steps:[/bold]")

    # Configuration info
    console.print("")
    console.print("📝 [bold cyan]Configuration Files Updated:[/bold cyan]")
    console.print("   • [green].env files[/green] - API keys and service URLs")
    console.print("   • [green]config.yml[/green] - Model definitions and memory provider settings")
    console.print("")

    # Development Environment Setup
    console.print("1. Setup development environment (git hooks, testing):")
    console.print("   [cyan]make setup-dev[/cyan]")
    console.print("   [dim]This installs pre-commit hooks to run tests before pushing[/dim]")
    console.print("")

    # Service Management Commands
    console.print("2. Start all configured services:")
    console.print("   [cyan]uv run --with-requirements setup-requirements.txt python services.py start --all --build[/cyan]")
    console.print("")
    console.print("3. Or start individual services:")
    
    configured_services = []
    if 'advanced' in selected_services and 'advanced' not in failed_services:
        configured_services.append("backend")
    if 'speaker-recognition' in selected_services and 'speaker-recognition' not in failed_services:
        configured_services.append("speaker-recognition") 
    if 'asr-services' in selected_services and 'asr-services' not in failed_services:
        configured_services.append("asr-services")
    if 'openmemory-mcp' in selected_services and 'openmemory-mcp' not in failed_services:
        configured_services.append("openmemory-mcp")
    if 'langfuse' in selected_services and 'langfuse' not in failed_services:
        configured_services.append("langfuse")

    # LangFuse prompt management info
    if langfuse_mode == 'local' and 'langfuse' not in failed_services:
        console.print("")
        console.print("[bold cyan]Prompt Management:[/bold cyan] Once services are running, edit AI prompts at:")
        console.print("   [link=http://localhost:3002/project/chronicle/prompts]http://localhost:3002/project/chronicle/prompts[/link]")
    elif langfuse_mode == 'external' and langfuse_host:
        console.print("")
        console.print(f"[bold cyan]Prompt Management:[/bold cyan] Edit AI prompts at your LangFuse instance:")
        console.print(f"   {langfuse_host}")

    if configured_services:
        service_list = " ".join(configured_services)
        console.print(f"   [cyan]uv run --with-requirements setup-requirements.txt python services.py start {service_list}[/cyan]")
    
    console.print("")
    console.print("3. Check service status:")
    console.print("   [cyan]uv run --with-requirements setup-requirements.txt python services.py status[/cyan]")
    
    console.print("")
    console.print("4. Stop services when done:")
    console.print("   [cyan]uv run --with-requirements setup-requirements.txt python services.py stop --all[/cyan]")
    
    console.print(f"\n🚀 [bold]Enjoy Chronicle![/bold]")
    
    # Show individual service usage
    console.print(f"\n💡 [dim]Tip: You can also setup services individually:[/dim]")
    console.print(f"[dim]   cd backends/advanced && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]")
    console.print(f"[dim]   cd extras/speaker-recognition && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]")
    console.print(f"[dim]   cd extras/asr-services && uv run --with-requirements ../../setup-requirements.txt python init.py[/dim]")

if __name__ == "__main__":
    main()