#!/usr/bin/env python3
"""
Chronicle LangFuse Setup Script
Auto-generates secrets and configures LangFuse for observability & prompt management
"""

import argparse
import os
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import mask_value, prompt_with_existing_masked, read_env_value

console = Console()


def print_header(title: str):
    """Print a colorful header"""
    console.print()
    panel = Panel(
        Text(title, style="cyan bold"),
        style="cyan",
        expand=False
    )
    console.print(panel)
    console.print()


def print_section(title: str):
    """Print a section header"""
    console.print()
    console.print(f"[magenta]► {title}[/magenta]")
    console.print("[magenta]" + "─" * len(f"► {title}") + "[/magenta]")


def backup_existing_env():
    """Backup existing .env file"""
    env_path = Path(".env")
    if env_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f".env.backup.{timestamp}"
        shutil.copy2(env_path, backup_path)
        console.print(f"[blue][INFO][/blue] Backed up existing .env file to {backup_path}")


def generate_secret(length: int = 32) -> str:
    """Generate a cryptographically secure hex secret"""
    return secrets.token_hex(length)


def run(args):
    """Run the LangFuse setup"""
    print_header("LangFuse Setup - Observability & Prompt Management")
    console.print("Configuring LangFuse for LLM tracing and prompt management")
    console.print()

    env_path = Path(".env")
    env_template = Path(".env.template")

    # --- Internal secrets (auto-generate if not already set) ---
    print_section("Internal Secrets")

    existing_salt = read_env_value(".env", "LANGFUSE_SALT")
    if existing_salt:
        salt = existing_salt
        console.print(f"[green][PRESERVED][/green] LANGFUSE_SALT: {mask_value(salt)}")
    else:
        salt = generate_secret(16)
        console.print(f"[green][GENERATED][/green] LANGFUSE_SALT: {mask_value(salt)}")

    existing_enc_key = read_env_value(".env", "LANGFUSE_ENCRYPTION_KEY")
    if existing_enc_key and existing_enc_key != "0000000000000000000000000000000000000000000000000000000000000000":
        enc_key = existing_enc_key
        console.print(f"[green][PRESERVED][/green] LANGFUSE_ENCRYPTION_KEY: {mask_value(enc_key)}")
    else:
        enc_key = generate_secret(32)
        console.print(f"[green][GENERATED][/green] LANGFUSE_ENCRYPTION_KEY: {mask_value(enc_key)}")

    existing_nextauth = read_env_value(".env", "LANGFUSE_NEXTAUTH_SECRET")
    if existing_nextauth and existing_nextauth != "mysecret":
        nextauth_secret = existing_nextauth
        console.print(f"[green][PRESERVED][/green] LANGFUSE_NEXTAUTH_SECRET: {mask_value(nextauth_secret)}")
    else:
        nextauth_secret = generate_secret(32)
        console.print(f"[green][GENERATED][/green] LANGFUSE_NEXTAUTH_SECRET: {mask_value(nextauth_secret)}")

    # --- Project API keys (auto-generate if not already set) ---
    print_section("Project API Keys")

    existing_pub_key = read_env_value(".env", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY")
    if existing_pub_key:
        public_key = existing_pub_key
        console.print(f"[green][PRESERVED][/green] Public key: {mask_value(public_key)}")
    else:
        public_key = f"pk-lf-{secrets.token_hex(16)}"
        console.print(f"[green][GENERATED][/green] Public key: {mask_value(public_key)}")

    existing_sec_key = read_env_value(".env", "LANGFUSE_INIT_PROJECT_SECRET_KEY")
    if existing_sec_key:
        secret_key = existing_sec_key
        console.print(f"[green][PRESERVED][/green] Secret key: {mask_value(secret_key)}")
    else:
        secret_key = f"sk-lf-{secrets.token_hex(16)}"
        console.print(f"[green][GENERATED][/green] Secret key: {mask_value(secret_key)}")

    # --- Admin user credentials ---
    print_section("Admin User")

    admin_email = getattr(args, 'admin_email', None) or ""
    admin_password = getattr(args, 'admin_password', None) or ""

    if admin_email:
        console.print(f"[green][FROM WIZARD][/green] Admin email: {admin_email}")
    else:
        existing_email = read_env_value(".env", "LANGFUSE_INIT_USER_EMAIL")
        admin_email = prompt_with_existing_masked(
            prompt_text="LangFuse admin email",
            existing_value=existing_email,
            placeholders=[""],
            is_password=False,
            default="admin@example.com"
        )

    if admin_password:
        console.print(f"[green][FROM WIZARD][/green] Admin password: {mask_value(admin_password)}")
    else:
        existing_password = read_env_value(".env", "LANGFUSE_INIT_USER_PASSWORD")
        admin_password = prompt_with_existing_masked(
            prompt_text="LangFuse admin password",
            existing_value=existing_password,
            placeholders=[""],
            is_password=True,
            default=""
        )

    # --- Write .env file ---
    print_section("Writing Configuration")

    backup_existing_env()

    if env_template.exists():
        shutil.copy2(env_template, env_path)
        console.print("[blue][INFO][/blue] Copied .env.template to .env")
    else:
        env_path.touch(mode=0o600)

    env_path_str = str(env_path)

    config = {
        "LANGFUSE_SALT": salt,
        "LANGFUSE_ENCRYPTION_KEY": enc_key,
        "LANGFUSE_NEXTAUTH_SECRET": nextauth_secret,
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": public_key,
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": secret_key,
        "LANGFUSE_INIT_ORG_ID": "chronicle",
        "LANGFUSE_INIT_ORG_NAME": "Chronicle",
        "LANGFUSE_INIT_PROJECT_ID": "chronicle",
        "LANGFUSE_INIT_PROJECT_NAME": "Chronicle",
        "LANGFUSE_INIT_USER_EMAIL": admin_email,
        "LANGFUSE_INIT_USER_NAME": "Admin",
        "LANGFUSE_INIT_USER_PASSWORD": admin_password,
    }

    for key, value in config.items():
        if value:
            set_key(env_path_str, key, value)

    os.chmod(env_path, 0o600)
    console.print("[green][SUCCESS][/green] .env file configured successfully")

    # --- Summary ---
    print_section("Configuration Summary")
    console.print()

    table = Table(title="LangFuse Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Web UI", "http://localhost:3002")
    table.add_row("Admin Email", admin_email)
    table.add_row("Public Key", mask_value(public_key))
    table.add_row("Secret Key", mask_value(secret_key))

    console.print(table)
    console.print()
    console.print("[green][SUCCESS][/green] LangFuse setup complete!")

    # Return keys for wizard to pass to backend
    return {
        "public_key": public_key,
        "secret_key": secret_key,
    }


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="LangFuse Setup")
    parser.add_argument("--admin-email", help="Admin email (reuse from backend)")
    parser.add_argument("--admin-password", help="Admin password (reuse from backend)")

    args = parser.parse_args()

    run(args)


if __name__ == "__main__":
    main()
