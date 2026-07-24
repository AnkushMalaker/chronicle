#!/usr/bin/env python3
"""
Chronicle Vault Sync Setup Script (macOS companion device)
Interactive configuration for the vault-sync menu bar app: syncs your Chronicle
memory vault from an existing server to this machine for local viewing (Obsidian).

Tailscale is NOT required — any address this machine can reach works (Tailnet
name, LAN IP, public domain). The server side must be running with vault sync
enabled (see README.md "Server setup").
"""

import argparse
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import prompt_with_existing_masked, read_env_value

PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent.parent


class VaultSyncSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()
        self.backend_env_path = REPO_ROOT / "backends" / "advanced" / ".env"
        # Client configuration is consolidated in the repository-root .env, shared
        # by all of Chronicle's native client components (tray, vault sync, …).
        self.env_path = REPO_ROOT / ".env"

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

    def read_existing_env_value(self, key: str) -> Optional[str]:
        return read_env_value(str(self.env_path), key)

    def read_backend_env_value(self, key: str) -> Optional[str]:
        if self.backend_env_path.exists():
            return read_env_value(str(self.backend_env_path), key)
        return None

    # --- prerequisites --------------------------------------------------------

    def check_platform(self) -> bool:
        if sys.platform == "darwin":
            return True
        self.console.print(
            f"[red][ERROR][/red] The vault-sync menu bar app is macOS-only for now "
            f"(this is {platform.system()}).\n"
            "  Linux/Windows companion support is planned. On other platforms you can\n"
            "  still pair a plain Syncthing manually against the backend's "
            "/api/vault-sync broker."
        )
        return False

    def ensure_syncthing(self) -> bool:
        """Find the syncthing binary, offering a Homebrew install if it's missing."""
        found = shutil.which("syncthing") or next(
            (
                str(p)
                for p in (
                    Path("/opt/homebrew/bin/syncthing"),
                    Path("/usr/local/bin/syncthing"),
                )
                if p.exists()
            ),
            None,
        )
        if found:
            self.console.print(f"[green][SUCCESS][/green] syncthing found: {found}")
            return True

        self.console.print(
            "[yellow][WARNING][/yellow] syncthing is not installed (the app needs it "
            "as its sync engine)"
        )
        if shutil.which("brew") and Confirm.ask(
            "Install syncthing with Homebrew now?", default=True
        ):
            result = subprocess.run(["brew", "install", "syncthing"])
            if result.returncode == 0 and shutil.which("syncthing"):
                self.console.print("[green][SUCCESS][/green] syncthing installed")
                return True
            self.console.print("[red][ERROR][/red] Homebrew install failed")
        self.console.print(
            "  Install it manually: [cyan]brew install syncthing[/cyan] "
            "(or https://syncthing.net/downloads/)"
        )
        return False

    def ensure_uv(self) -> bool:
        if shutil.which("uv"):
            return True
        self.console.print(
            "[yellow][WARNING][/yellow] 'uv' not found on PATH — the app is launched "
            "with uv.\n"
            "  Install it: [cyan]curl -LsSf https://astral.sh/uv/install.sh | sh[/cyan]"
        )
        return False

    # --- configuration --------------------------------------------------------

    def setup_backend_url(self):
        self.print_section("Backend Connection")
        self.console.print(
            "URL of your Chronicle server. Tailscale is not required — use whatever\n"
            "address this machine can reach: a Tailnet name "
            "(https://my-host.ts.net), a\n"
            "LAN address (http://192.168.1.20:8000), or a public domain."
        )
        self.console.print(
            "[dim]Tip: for a LAN server with a self-signed HTTPS certificate, use the "
            "plain-HTTP\nbackend port (http://<server-ip>:8000) — this app is not a "
            "browser and doesn't\nneed HTTPS.[/dim]"
        )
        self.console.print()

        if getattr(self.args, "backend_url", None):
            backend_url = self.args.backend_url
            self.console.print(
                f"[green][SUCCESS][/green] Backend URL from command line: {backend_url}"
            )
        else:
            existing = self.read_existing_env_value("BACKEND_URL")
            backend_url = self.prompt_value("Backend URL", existing or "")
        self.config["BACKEND_URL"] = backend_url.rstrip("/")

    def setup_auth_credentials(self):
        self.print_section("Authentication")
        self.console.print(
            "Your Chronicle login (same account as the web dashboard). The vault that\n"
            "syncs here is this account's vault."
        )
        self.console.print()

        backend_email = self.read_backend_env_value("ADMIN_EMAIL")
        backend_password = self.read_backend_env_value("ADMIN_PASSWORD")

        if getattr(self.args, "username", None):
            username = self.args.username
            self.console.print("[green][SUCCESS][/green] Username from command line")
        else:
            existing = self.read_existing_env_value("AUTH_USERNAME")
            default_user = existing or backend_email or ""
            username = self.prompt_value("Auth username (email)", default_user)
        self.config["AUTH_USERNAME"] = username

        if getattr(self.args, "password", None):
            password = self.args.password
            self.console.print("[green][SUCCESS][/green] Password from command line")
        else:
            existing_pw = self.read_existing_env_value("AUTH_PASSWORD")
            if not existing_pw and backend_password:
                existing_pw = backend_password
                self.console.print(
                    "[blue][INFO][/blue] Using admin password from backend .env"
                )
            password = prompt_with_existing_masked(
                prompt_text="Auth password",
                existing_value=existing_pw,
                is_password=True,
            )
        self.config["AUTH_PASSWORD"] = password

    def setup_local_config(self):
        self.print_section("Local Vault")
        self.console.print(
            "Where the vault lives on this machine (open this folder in Obsidian).\n"
            "You can change it later from the menu bar app."
        )
        self.console.print()

        existing_dir = self.read_existing_env_value("LOCAL_VAULT_DIR")
        vault_dir = self.prompt_value(
            "Local vault folder", existing_dir or "~/ChronicleVault"
        )
        self.config["LOCAL_VAULT_DIR"] = vault_dir

        existing_name = self.read_existing_env_value("DEVICE_NAME")
        device_name = self.prompt_value(
            "Device name (how this machine appears on the server)",
            existing_name or socket.gethostname().split(".")[0],
        )
        self.config["DEVICE_NAME"] = device_name

    # --- verification ---------------------------------------------------------

    def verify_backend(self) -> bool:
        """Log in and hit the pairing broker so misconfiguration surfaces now,
        with an actionable message, instead of as a menu-bar error icon later."""
        self.print_section("Verifying backend")
        url = self.config["BACKEND_URL"]
        if not url:
            self.console.print(
                "[yellow][WARNING][/yellow] No backend URL set — skipping verification"
            )
            return False

        try:
            resp = requests.post(
                f"{url}/auth/jwt/login",
                data={
                    "username": self.config["AUTH_USERNAME"],
                    "password": self.config["AUTH_PASSWORD"],
                },
                timeout=10,
            )
        except requests.exceptions.SSLError:
            self.console.print(
                f"[red][ERROR][/red] TLS verification failed for {url} — the server "
                "likely uses a\n  self-signed certificate. Use the plain-HTTP backend "
                "port instead, e.g.\n  "
                f"[cyan]{url.replace('https://', 'http://')}:8000[/cyan]"
            )
            return False
        except requests.exceptions.RequestException as e:
            self.console.print(
                f"[red][ERROR][/red] Cannot reach the backend at {url}: {e}"
            )
            return False

        if resp.status_code != 200:
            self.console.print(
                f"[red][ERROR][/red] Login failed (HTTP {resp.status_code}) — check "
                "AUTH_USERNAME/AUTH_PASSWORD"
            )
            return False
        token = resp.json().get("access_token")
        self.console.print("[green][SUCCESS][/green] Logged in to backend")

        info_resp = requests.get(
            f"{url}/api/vault-sync/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if info_resp.status_code == 503:
            self.console.print(
                "[red][ERROR][/red] The server doesn't have vault sync enabled yet. "
                "On the server:\n"
                "  1. Add to backends/advanced/.env:\n"
                "     [cyan]VAULT_SYNC_API_KEY=<any long random string>[/cyan]\n"
                "     [cyan]VAULT_SYNC_ADDRESS=tcp://<server-address>:22000[/cyan] "
                "(comma-separate several)\n"
                "  2. [cyan]docker compose --profile vault-sync up -d "
                "vault-syncthing[/cyan]\n"
                "  3. [cyan]docker compose up -d --force-recreate "
                "chronicle-backend[/cyan]"
            )
            return False
        if info_resp.status_code != 200:
            self.console.print(
                f"[red][ERROR][/red] Pairing broker error (HTTP {info_resp.status_code}): "
                f"{info_resp.text[:200]}"
            )
            return False

        info = info_resp.json()
        self.console.print(
            f"[green][SUCCESS][/green] Pairing broker ready — server device "
            f"{info['server_device_id'][:7]}…, folder {info['folder_id']}"
        )
        if not info.get("sync_address"):
            self.console.print(
                "[yellow][WARNING][/yellow] The server has no VAULT_SYNC_ADDRESS set — "
                "sync will rely on\n  Syncthing discovery/relays. For LAN or corporate "
                "networks set an explicit\n  address on the server (comma-separate "
                "several, e.g. Tailnet + LAN IP)."
            )
        return True

    # --- output ---------------------------------------------------------------

    def generate_env_file(self):
        if self.env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.env_path.with_name(f".env.backup.{timestamp}")
            shutil.copy2(self.env_path, backup_path)
            self.console.print(
                f"[blue][INFO][/blue] Backed up existing .env to {backup_path.name}"
            )

        template = REPO_ROOT / ".env.template"
        if not self.env_path.exists():
            if template.exists():
                shutil.copy2(template, self.env_path)
            else:
                self.env_path.touch()
        self.env_path.chmod(0o600)

        for key, value in self.config.items():
            if value:
                set_key(str(self.env_path), key, value)
        self.env_path.chmod(0o600)
        self.console.print(
            "[green][SUCCESS][/green] .env configured with secure permissions"
        )

    def offer_install(self):
        self.print_section("Run it")
        try:
            install_now = Confirm.ask(
                "Install as a login item now? (menu bar app starts automatically)",
                default=True,
            )
        except EOFError:
            install_now = False

        if install_now:
            result = subprocess.run(
                ["uv", "run", "chronicle-tray", "install"],
                cwd=REPO_ROOT / "extras" / "chronicle-tray",
            )
            if result.returncode == 0:
                self.console.print(
                    "\n[green][SUCCESS][/green] Installed — look for the ◈ icon in "
                    "your menu bar."
                )
                return
            self.console.print("[red][ERROR][/red] Install failed; run it manually:")

        self.console.print()
        self.console.print(
            "Run in the foreground:      "
            "[cyan]cd ../chronicle-tray && uv run chronicle-tray[/cyan]"
        )
        self.console.print(
            "Install as a login item:    "
            "[cyan]cd ../chronicle-tray && uv run chronicle-tray install[/cyan]"
        )

    def show_summary(self):
        self.print_section("Configuration Summary")
        self.console.print()
        self.console.print(f"  Backend URL:  {self.config.get('BACKEND_URL', '')}")
        self.console.print(f"  Account:      {self.config.get('AUTH_USERNAME', '')}")
        self.console.print(f"  Local vault:  {self.config.get('LOCAL_VAULT_DIR', '')}")
        self.console.print(f"  Device name:  {self.config.get('DEVICE_NAME', '')}")

    def run(self):
        self.print_header("Chronicle Vault Sync Setup (macOS)")
        self.console.print(
            "Sync your Chronicle memory vault to this machine and browse it in "
            "Obsidian.\nThe app lives in the menu bar and pairs with the server "
            "automatically."
        )

        try:
            if not self.check_platform():
                sys.exit(1)
            self.ensure_syncthing()
            self.ensure_uv()

            self.setup_backend_url()
            self.setup_auth_credentials()
            self.setup_local_config()

            self.print_header("Configuration Complete!")
            self.generate_env_file()
            self.show_summary()

            verified = self.verify_backend()
            self.offer_install()

            self.console.print()
            if verified:
                self.console.print(
                    "[green][SUCCESS][/green] Vault sync setup complete!"
                )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] Setup finished but verification did "
                    "not pass — fix the\nissue above, then use the menu bar app's "
                    "'Sync Now / Re-pair' (or rerun this setup)."
                )
        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Chronicle Vault Sync Setup")
    parser.add_argument("--backend-url", help="Backend URL (default: prompt user)")
    parser.add_argument("--username", help="Auth username/email (default: prompt user)")
    parser.add_argument("--password", help="Auth password (default: prompt user)")
    args = parser.parse_args()

    VaultSyncSetup(args).run()


if __name__ == "__main__":
    main()
