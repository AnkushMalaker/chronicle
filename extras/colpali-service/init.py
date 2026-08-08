#!/usr/bin/env python3
"""
Chronicle ColPali Service Setup
Interactive configuration for visual search over saved screenshots.
"""

import argparse
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from chronicle_setup import detect_cuda_version as _detect_cuda_version
from chronicle_setup import read_env_value
from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

MODELS = {
    "vidore/colSmol-256M": (
        "colSmol-256M — ~0.5-1 GB VRAM. Default: fits beside a desktop's own "
        "graphics use, which a 12 GB card being used for anything else needs."
    ),
    "vidore/colqwen2.5-v0.2": (
        "ColQwen2.5 — ~7 GB VRAM. Better retrieval, but leaves little headroom "
        "on a 12 GB card that is also driving a display."
    ),
}
DEFAULT_MODEL = "vidore/colSmol-256M"
DEFAULT_PORT = "8790"
# Embedding is a trickle workload, so holding VRAM permanently is the wrong default.
DEFAULT_IDLE_UNLOAD = "900"

SERVICE_DIR = Path(__file__).parent
ENV_PATH = SERVICE_DIR / ".env"

console = Console()


def backup_env() -> Optional[Path]:
    if not ENV_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = SERVICE_DIR / f".env.backup.{stamp}"
    shutil.copy2(ENV_PATH, backup)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure the ColPali service")
    parser.add_argument("--hf-token", default=None)
    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    console.print(
        Panel.fit(
            "[bold cyan]Chronicle ColPali Service[/bold cyan]\n"
            "Visual search over screenshots you saved — for the queries that\n"
            "defeat both a written description and OCR.\n\n"
            "[dim]Additive: screenshot search works without this running, so the\n"
            "node hosting it may be asleep.[/dim]",
            border_style="cyan",
        )
    )

    table = Table(title="Models", show_header=True, header_style="bold magenta")
    table.add_column("Model")
    table.add_column("Notes")
    for name, description in MODELS.items():
        table.add_row(name, description)
    console.print(table)

    model = DEFAULT_MODEL
    port = DEFAULT_PORT
    idle_unload = DEFAULT_IDLE_UNLOAD
    if not args.non_interactive:
        model = Prompt.ask("Model", choices=list(MODELS.keys()), default=DEFAULT_MODEL)
        port = Prompt.ask("Service port", default=DEFAULT_PORT)
        if not Confirm.ask("Unload the model when idle to free VRAM?", default=True):
            idle_unload = "0"

    cuda_version = _detect_cuda_version() or "cu126"
    hf_token = args.hf_token or read_env_value(ENV_PATH, "HF_TOKEN") or ""

    backup = backup_env()
    if backup:
        console.print(f"[dim]Backed up existing .env to {backup.name}[/dim]")
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), "COLPALI_MODEL", model, quote_mode="never")
    set_key(str(ENV_PATH), "COLPALI_PORT", port, quote_mode="never")
    set_key(
        str(ENV_PATH),
        "COLPALI_IDLE_UNLOAD_SECONDS",
        idle_unload,
        quote_mode="never",
    )
    set_key(str(ENV_PATH), "PYTORCH_CUDA_VERSION", cuda_version, quote_mode="never")
    set_key(str(ENV_PATH), "HF_TOKEN", hf_token, quote_mode="never")

    console.print(
        Panel.fit(
            f"[green]Configured[/green]  model=[cyan]{model}[/cyan]  "
            f"port=[cyan]{port}[/cyan]  cuda=[cyan]{cuda_version}[/cyan]\n\n"
            "Start it with:\n"
            "  [bold]cd extras/colpali-service && docker compose up colpali -d --build[/bold]\n"
            "  [dim](podman-compose on a podman host)[/dim]\n\n"
            "Check it:\n"
            f"  [bold]curl http://localhost:{port}/health[/bold]",
            border_style="green",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
