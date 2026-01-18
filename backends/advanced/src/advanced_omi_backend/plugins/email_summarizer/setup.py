#!/usr/bin/env python3
"""
Email Summarizer Plugin Setup Wizard

Configures SMTP credentials and plugin settings.
Follows Chronicle's clean configuration architecture:
- Secrets → backends/advanced/.env
- Non-secret settings → plugins/email_summarizer/config.yml
- Orchestration → config/plugins.yml
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import set_key
from rich.console import Console
from rich.prompt import Confirm

# Add repo root to path for setup_utils import
project_root = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(project_root))

from setup_utils import (
    prompt_with_existing_masked,
    prompt_value
)

console = Console()


def update_plugins_yml_with_env_refs():
    """
    Update config/plugins.yml with environment variable references.
    This ensures secrets are NOT hardcoded in plugins.yml.
    """
    plugins_yml_path = project_root / "config" / "plugins.yml"

    # Load existing or create from template
    if plugins_yml_path.exists():
        with open(plugins_yml_path, 'r') as f:
            config = yaml.safe_load(f) or {}
    else:
        # Copy from template
        template_path = project_root / "config" / "plugins.yml.template"
        if template_path.exists():
            with open(template_path, 'r') as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {'plugins': {}}

    # Ensure structure exists
    if 'plugins' not in config:
        config['plugins'] = {}

    # Build plugin config with env var references (NOT actual values!)
    plugin_config = {
        'enabled': False,  # Let user enable manually or prompt
        'events': ['conversation.complete'],
        'condition': {'type': 'always'},
        # Use env var references - these get expanded at runtime
        'smtp_host': '${SMTP_HOST:-smtp.gmail.com}',
        'smtp_port': '${SMTP_PORT:-587}',
        'smtp_username': '${SMTP_USERNAME}',
        'smtp_password': '${SMTP_PASSWORD}',
        'smtp_use_tls': '${SMTP_USE_TLS:-true}',
        'from_email': '${FROM_EMAIL}',
        'from_name': '${FROM_NAME:-Chronicle AI}',
        # Non-secret settings (literal values OK)
        'subject_prefix': 'Conversation Summary',
        'summary_max_sentences': 3,
        'include_conversation_id': True,
        'include_duration': True
    }

    # Update or create plugin entry
    config['plugins']['email_summarizer'] = plugin_config

    # Backup existing file
    if plugins_yml_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = plugins_yml_path.parent / f"plugins.yml.backup.{timestamp}"
        shutil.copy(plugins_yml_path, backup_path)
        console.print(f"[dim]Backed up existing plugins.yml to {backup_path.name}[/dim]")

    # Write updated config
    plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plugins_yml_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    console.print("[green]✅ Updated config/plugins.yml with environment variable references[/green]")

    return plugins_yml_path


def main():
    """Interactive setup for Email Summarizer plugin"""
    console.print("\n📧 [bold cyan]Email Summarizer Plugin Setup[/bold cyan]")
    console.print("This plugin sends email summaries when conversations complete.\n")

    # Path to main backend .env file
    env_path = str(project_root / "backends" / "advanced" / ".env")

    # SMTP Configuration
    console.print("[bold]SMTP Configuration[/bold]")
    console.print("[dim]For Gmail: Use App Password (Settings > Security > 2FA > App Passwords)[/dim]\n")

    smtp_host = prompt_with_existing_masked(
        prompt_text="SMTP Host",
        env_file_path=env_path,
        env_key="SMTP_HOST",
        placeholders=['your-smtp-host-here'],
        is_password=False,
        default="smtp.gmail.com"
    )

    smtp_port = prompt_value("SMTP Port", default="587")

    smtp_username = prompt_with_existing_masked(
        prompt_text="SMTP Username (your email)",
        env_file_path=env_path,
        env_key="SMTP_USERNAME",
        placeholders=['your-email@example.com'],
        is_password=False
    )

    smtp_password = prompt_with_existing_masked(
        prompt_text="SMTP Password (App Password)",
        env_file_path=env_path,
        env_key="SMTP_PASSWORD",
        placeholders=['your-password-here', 'your-app-password-here'],
        is_password=True  # Shows masked existing value
    )

    # Remove spaces from app password (Google adds spaces when copying)
    smtp_password = smtp_password.replace(" ", "")

    smtp_use_tls = prompt_value("Use TLS? (true/false)", default="true")

    # Email sender configuration
    from_email = prompt_with_existing_masked(
        prompt_text="From Email",
        env_file_path=env_path,
        env_key="FROM_EMAIL",
        placeholders=['noreply@example.com'],
        is_password=False,
        default=smtp_username  # Default to SMTP username
    )

    from_name = prompt_value("From Name", default="Chronicle AI")

    # Save secrets to .env
    console.print("\n💾 [bold]Saving credentials to .env...[/bold]")

    set_key(env_path, "SMTP_HOST", smtp_host)
    set_key(env_path, "SMTP_PORT", smtp_port)
    set_key(env_path, "SMTP_USERNAME", smtp_username)
    set_key(env_path, "SMTP_PASSWORD", smtp_password)
    set_key(env_path, "SMTP_USE_TLS", smtp_use_tls)
    set_key(env_path, "FROM_EMAIL", from_email)
    set_key(env_path, "FROM_NAME", from_name)

    console.print("[green]✅ SMTP credentials saved to backends/advanced/.env[/green]")

    # Auto-update plugins.yml with env var references
    console.print("\n📝 [bold]Updating plugin configuration...[/bold]")
    plugins_yml_path = update_plugins_yml_with_env_refs()

    # Prompt to enable plugin
    enable_now = Confirm.ask("\nEnable email_summarizer plugin now?", default=True)
    if enable_now:
        with open(plugins_yml_path, 'r') as f:
            config = yaml.safe_load(f)
        config['plugins']['email_summarizer']['enabled'] = True
        with open(plugins_yml_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        console.print("[green]✅ Plugin enabled in config/plugins.yml[/green]")

    console.print("\n[bold cyan]✅ Email Summarizer configured successfully![/bold cyan]")
    console.print("\n[bold]Configuration saved to:[/bold]")
    console.print("  • [green]backends/advanced/.env[/green] - SMTP credentials (secrets)")
    console.print("  • [green]config/plugins.yml[/green] - Plugin orchestration (env var references)")
    console.print()

    if not enable_now:
        console.print("[bold]To enable later:[/bold]")
        console.print("  Edit config/plugins.yml and set: enabled: true")
        console.print()

    console.print("[bold]Restart backend to apply:[/bold]")
    console.print("  [dim]cd backends/advanced && docker compose restart[/dim]")
    console.print()
    console.print("[yellow]⚠️  SECURITY: Never paste actual passwords in config/plugins.yml![/yellow]")
    console.print("[yellow]    Secrets go in .env, YAML files use ${ENV_VAR} references.[/yellow]")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Setup cancelled by user[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Error during setup: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)
