#!/usr/bin/env python3
"""Guided host-native setup for a Chronicle ScreenPipe capture node."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt


console = Console()
PROJECT = Path(__file__).resolve().parent
SYSTEMD_USER_DIR = Path.home() / ".config/systemd/user"


def screenpipe_command() -> str | None:
    return shutil.which("screenpipe")


def write_screenpipe_unit(binary: str, api_key: str) -> Path:
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    path = SYSTEMD_USER_DIR / "screenpipe.service"
    args = [
        binary,
        "record",
        "--audio-transcription-engine", "disabled",
        "--use-system-default-audio", "true",
        "--use-all-monitors", "true",
        "--use-pii-removal", "true",
        "--disable-keyboard-capture",
        "--disable-clipboard-capture",
        "--prioritize-input-latency",
        "--pause-on-drm-content",
        "--disable-meeting-detector",
        "--disable-telemetry",
        "--video-quality", "balanced",
        "--retention-days", "90",
        "--retention-mode", "media",
        "--api-auth", "true",
    ]
    path.write_text(
        "[Unit]\nDescription=ScreenPipe local recorder for Chronicle\n"
        "After=graphical-session.target\n\n[Service]\nType=simple\n"
        f"Environment=SCREENPIPE_API_KEY={api_key}\n"
        f"ExecStart={' '.join(args)}\nRestart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure a Chronicle capture node")
    parser.add_argument("--backend")
    args = parser.parse_args()

    console.print("\n🖥️  [bold cyan]Chronicle capture node[/bold cyan]")
    binary = screenpipe_command()
    if not binary:
        console.print(
            "[red]✗ ScreenPipe is not installed.[/red] Install it independently, verify "
            "[cyan]screenpipe record --help[/cyan], then rerun this wizard."
        )
        raise SystemExit(1)
    console.print(f"[green]✅[/green] ScreenPipe detected: [cyan]{binary}[/cyan]")

    backend = Prompt.ask("Chronicle backend URL", default=args.backend or "http://127.0.0.1:8000")
    console.print(
        "Open Chronicle → Timeline → Sources and create a pairing code. "
        "The code expires after 10 minutes."
    )
    code = Prompt.ask("Pairing code").strip()
    if not code:
        raise SystemExit("pairing code is required")

    api_key = secrets.token_urlsafe(32)
    run(
        "uv", "run", "--project", str(PROJECT), "chronicle-screenpipe", "pair",
        "--backend", backend,
        "--code", code,
        "--screenpipe-dir", str(Path.home() / ".screenpipe"),
        "--screenpipe-url", "http://127.0.0.1:3030",
        "--screenpipe-token", api_key,
    )
    write_screenpipe_unit(binary, api_key)
    run("uv", "run", "--project", str(PROJECT), "chronicle-screenpipe", "install-service")
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "screenpipe.service")
    run("systemctl", "--user", "restart", "chronicle-screenpipe.service")

    if Confirm.ask("Check service status now?", default=True):
        run(
            "systemctl", "--user", "--no-pager", "--full", "status",
            "screenpipe.service", "chronicle-screenpipe.service",
        )
    console.print(
        "\n[green]✅ Capture node connected.[/green] Activity should appear in "
        "Chronicle Timeline after ScreenPipe records a stable app/window span."
    )


if __name__ == "__main__":
    main()
