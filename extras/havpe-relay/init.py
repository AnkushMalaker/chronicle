#!/usr/bin/env python3
"""
Chronicle HAVPE Relay Setup Script
Interactive configuration for the ESP32 Voice-PE TCP-to-WebSocket relay.
"""

import argparse
import getpass
import os
import shutil
import socket
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
from setup_utils import mask_value, prompt_with_existing_masked, read_env_value


class HavpeRelaySetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()
        self.backend_env_path = (
            Path(__file__).resolve().parent.parent.parent
            / "backends"
            / "advanced"
            / ".env"
        )

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
                if password:
                    return password
                self.console.print("[yellow][WARNING][/yellow] Password is required")
            except (EOFError, KeyboardInterrupt):
                self.console.print("[red][ERROR][/red] Password is required")
                sys.exit(1)

    def read_existing_env_value(self, key: str) -> str:
        """Read a value from existing .env file"""
        return read_env_value(".env", key)

    def read_backend_env_value(self, key: str) -> str:
        """Read a value from the backend's .env file"""
        if self.backend_env_path.exists():
            return read_env_value(str(self.backend_env_path), key)
        return None

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

    def _try_discover_backend(self) -> str | None:
        """Try to auto-discover the Chronicle backend via minidisc on Tailnet."""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            # Lazy import: sys.path-dependent (repo-root discovery.py, inserted above)
            from discovery import CHRONICLE_BACKEND, discover_service

            url = discover_service(CHRONICLE_BACKEND)
            if url:
                self.console.print(
                    f"[green][SUCCESS][/green] Auto-discovered backend on Tailnet: {url}"
                )
            return url
        except Exception:
            return None

    def setup_backend_urls(self):
        """Configure backend URL and WebSocket URL"""
        self.print_section("Backend Connection")
        self.console.print("Configure how the relay connects to the Chronicle backend")
        self.console.print()

        default_http = "http://host.docker.internal:8000"
        default_ws = "ws://host.docker.internal:8000"

        # Try auto-discovery via minidisc first
        discovered = self._try_discover_backend()
        if discovered:
            default_http = discovered

        # Check CLI args first
        if hasattr(self.args, "backend_url") and self.args.backend_url:
            backend_url = self.args.backend_url
            self.console.print(
                f"[green][SUCCESS][/green] Backend URL configured from command line: {backend_url}"
            )
        else:
            # Check existing .env
            existing = self.read_existing_env_value("BACKEND_URL")
            if existing:
                default_http = existing
            backend_url = self.prompt_value("Backend HTTP URL", default_http)

        self.config["BACKEND_URL"] = backend_url

        # Auto-derive WS URL from HTTP URL
        auto_ws = backend_url.replace("https://", "wss://").replace("http://", "ws://")

        if hasattr(self.args, "backend_ws_url") and self.args.backend_ws_url:
            ws_url = self.args.backend_ws_url
            self.console.print(
                f"[green][SUCCESS][/green] Backend WS URL configured from command line: {ws_url}"
            )
        else:
            existing_ws = self.read_existing_env_value("BACKEND_WS_URL")
            if existing_ws:
                auto_ws = existing_ws
            ws_url = self.prompt_value("Backend WebSocket URL", auto_ws)

        self.config["BACKEND_WS_URL"] = ws_url

    def setup_auth_credentials(self):
        """Configure authentication credentials"""
        self.print_section("Authentication")
        self.console.print("Credentials for authenticating with the Chronicle backend")
        self.console.print()

        # Try to read defaults from backend .env
        backend_email = self.read_backend_env_value("ADMIN_EMAIL")
        backend_password = self.read_backend_env_value("ADMIN_PASSWORD")

        # Username
        if hasattr(self.args, "username") and self.args.username:
            username = self.args.username
            self.console.print(
                f"[green][SUCCESS][/green] Username configured from command line"
            )
        else:
            existing = self.read_existing_env_value("AUTH_USERNAME")
            default_user = existing or backend_email or ""
            if default_user:
                username = self.prompt_value("Auth username (email)", default_user)
            else:
                username = self.prompt_value("Auth username (email)")

        self.config["AUTH_USERNAME"] = username

        # Password
        if hasattr(self.args, "password") and self.args.password:
            password = self.args.password
            self.console.print(
                f"[green][SUCCESS][/green] Password configured from command line"
            )
        else:
            existing_pw = self.read_existing_env_value("AUTH_PASSWORD")
            # Fall back to backend admin password if no local password set
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

    def setup_device_config(self):
        """Configure device name and TCP port"""
        self.print_section("Device Configuration")
        self.console.print("Configure the relay's device identity and TCP listener")
        self.console.print()

        # Device name
        if hasattr(self.args, "device_name") and self.args.device_name:
            device_name = self.args.device_name
            self.console.print(
                f"[green][SUCCESS][/green] Device name configured from command line: {device_name}"
            )
        else:
            existing = self.read_existing_env_value("DEVICE_NAME")
            device_name = self.prompt_value("Device name", existing or "havpe")

        self.config["DEVICE_NAME"] = device_name

        # TCP port
        if hasattr(self.args, "tcp_port") and self.args.tcp_port:
            tcp_port = self.args.tcp_port
            self.console.print(
                f"[green][SUCCESS][/green] TCP port configured from command line: {tcp_port}"
            )
        else:
            existing = self.read_existing_env_value("TCP_PORT")
            tcp_port = self.prompt_value("TCP listen port", existing or "8989")

        self.config["TCP_PORT"] = tcp_port

    def setup_esphome_config(self):
        """Configure optional static ESPHome device IP"""
        self.print_section("ESPHome Device IP (Optional)")
        self.console.print(
            "If set, the relay always connects ESPHome API to this IP instead of"
        )
        self.console.print(
            "the TCP client's IP. Recommended when non-ESP32 clients also connect."
        )
        self.console.print()

        existing = self.read_existing_env_value("ESPHOME_DEVICE_IP")
        esphome_ip = self.prompt_value(
            "ESPHome device IP (leave blank to auto-detect from TCP client)",
            existing or "",
        )
        if esphome_ip:
            self.config["ESPHOME_DEVICE_IP"] = esphome_ip

    def setup_firmware_secrets(self):
        """Optionally configure ESP32 firmware secrets"""
        self.print_section("ESP32 Firmware Configuration (Optional)")
        self.console.print("Configure WiFi and relay address for ESPHome firmware")
        self.console.print()

        try:
            configure = Confirm.ask("Configure ESP32 firmware secrets?", default=False)
        except EOFError:
            configure = False

        if not configure:
            self.console.print("[blue][INFO][/blue] Skipping firmware configuration")
            return

        wifi_ssid = self.prompt_value("WiFi SSID")
        wifi_password = self.prompt_password("WiFi password")
        # Auto-detect LAN IP as default
        default_ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            default_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        relay_ip = self.prompt_value(
            "Relay IP address (LAN IP of this machine)", default_ip
        )

        # Backup existing secrets.yaml
        secrets_path = Path("firmware/secrets.yaml")
        template_path = Path("firmware/secrets.template.yaml")

        if secrets_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"firmware/secrets.yaml.backup.{timestamp}"
            shutil.copy2(secrets_path, backup_path)
            self.console.print(
                f"[blue][INFO][/blue] Backed up existing firmware/secrets.yaml to {backup_path}"
            )

        if not template_path.exists():
            self.console.print(
                "[yellow][WARNING][/yellow] firmware/secrets.template.yaml not found, creating secrets.yaml directly"
            )
            content = ""
        else:
            with open(template_path, "r") as f:
                content = f.read()

        # Replace placeholder values
        if content:
            content = content.replace('wifi_ssid: ""', f'wifi_ssid: "{wifi_ssid}"')
            content = content.replace(
                'wifi_password: ""', f'wifi_password: "{wifi_password}"'
            )
            content = content.replace(
                'relay_ip_address: ""', f'relay_ip_address: "{relay_ip}"'
            )
        else:
            content = (
                f"# ESPHome Secrets - Generated by init.py\n"
                f'wifi_ssid: "{wifi_ssid}"\n'
                f'wifi_password: "{wifi_password}"\n'
                f'relay_ip_address: "{relay_ip}"\n'
                f'api_encryption_key: ""\n'
                f'ota_password: ""\n'
            )

        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        with open(secrets_path, "w") as f:
            f.write(content)
        os.chmod(secrets_path, 0o600)

        self.console.print(
            "[green][SUCCESS][/green] firmware/secrets.yaml configured with secure permissions"
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
            if value:
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

        self.console.print(f"  Backend URL:    {self.config.get('BACKEND_URL', '')}")
        self.console.print(f"  Backend WS URL: {self.config.get('BACKEND_WS_URL', '')}")
        self.console.print(f"  Auth Username:  {self.config.get('AUTH_USERNAME', '')}")
        self.console.print(
            f"  Auth Password:  {'Configured' if self.config.get('AUTH_PASSWORD') else 'Not set'}"
        )
        self.console.print(f"  Device Name:    {self.config.get('DEVICE_NAME', '')}")
        self.console.print(f"  TCP Port:       {self.config.get('TCP_PORT', '')}")
        esphome_ip = self.config.get("ESPHOME_DEVICE_IP", "")
        self.console.print(
            f"  ESPHome IP:     {esphome_ip or '(auto-detect from TCP client)'}"
        )

    def show_next_steps(self):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        self.console.print("1. Start the HAVPE relay:")
        self.console.print("   [cyan]docker compose up --build -d[/cyan]")
        self.console.print()
        self.console.print("2. Or run directly (without Docker):")
        self.console.print("   [cyan]uv run python main.py[/cyan]")
        self.console.print()
        self.console.print("3. If you configured firmware, flash your ESP32:")
        self.console.print(
            "   [cyan]cd firmware && esphome run voice-chronicle.yaml[/cyan]"
        )

    def run(self):
        """Run the complete setup process"""
        self.print_header("HAVPE Relay Setup")
        self.console.print("Configure the ESP32 Voice-PE TCP-to-WebSocket relay")
        self.console.print()

        try:
            self.setup_backend_urls()
            self.setup_auth_credentials()
            self.setup_device_config()
            self.setup_esphome_config()
            self.setup_firmware_secrets()

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()

            # Show results
            self.show_summary()
            self.show_next_steps()

            self.console.print()
            self.console.print("[green][SUCCESS][/green] HAVPE Relay setup complete!")

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="HAVPE Relay Setup")
    parser.add_argument("--backend-url", help="Backend HTTP URL (default: prompt user)")
    parser.add_argument(
        "--backend-ws-url", help="Backend WebSocket URL (default: prompt user)"
    )
    parser.add_argument("--username", help="Auth username/email (default: prompt user)")
    parser.add_argument("--password", help="Auth password (default: prompt user)")
    parser.add_argument("--device-name", help="Device name (default: havpe)")
    parser.add_argument("--tcp-port", help="TCP listen port (default: 8989)")

    args = parser.parse_args()

    setup = HavpeRelaySetup(args)
    setup.run()


if __name__ == "__main__":
    main()
