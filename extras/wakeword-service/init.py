#!/usr/bin/env python3
"""Chronicle Hermes Wake-Word Service setup.

Configures the standalone acoustic wake-word detector. Writes a `.env` from the
template (preserving existing values) and warns if the trained model is missing.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

# Repo root for shared utilities.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import read_env_value  # noqa: E402

console = Console()
HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
ENV_TEMPLATE = HERE / ".env.template"
MODEL_PATH = HERE / "models" / "hey_hermes.onnx"


def configure(non_interactive: bool = False) -> None:
    """Create/update .env and report model status."""
    console.print(
        Panel.fit(
            "Hermes Acoustic Wake-Word Service",
            subtitle="standalone detector on the live audio stream",
        )
    )

    if not ENV_PATH.exists():
        if ENV_TEMPLATE.exists():
            shutil.copy(ENV_TEMPLATE, ENV_PATH)
            console.print(f"[green]Created {ENV_PATH} from template[/green]")
        else:
            ENV_PATH.touch()

    defaults = {
        "REDIS_URL": "redis://redis:6379/0",
        # Host 8770 is used by the tts/kittentts service; wakeword maps to host 8771.
        "WAKEWORD_PORT": "8771",
        "WAKEWORD_THRESHOLD": "0.9",
        "WAKEWORD_PATIENCE": "2",
        "WAKEWORD_DEBOUNCE_SECS": "3.0",
        "WAKEWORD_VAD_THRESHOLD": "0.5",
        "WAKEWORD_STOP_SECS": "2.0",
        "WAKEWORD_MAX_ARM_SECS": "15.0",
        "LOG_LEVEL": "INFO",
    }

    for key, default in defaults.items():
        existing = read_env_value(str(ENV_PATH), key)
        if non_interactive:
            value = existing or default
        else:
            value = Prompt.ask(key, default=existing or default)
        set_key(str(ENV_PATH), key, value, quote_mode="never")

    console.print(f"[green]Wrote configuration to {ENV_PATH}[/green]")

    if not MODEL_PATH.exists():
        console.print(
            Panel.fit(
                f"[yellow]Wake-word model not found at {MODEL_PATH}.[/yellow]\n"
                "Train it first:\n"
                "  cd training && ./fetch_datasets.sh\n"
                "  .venv-train/bin/nanowakeword -c hermes_config.yaml\n"
                "  cp trained_models/hermes/model/hermes.onnx ../models/hermes.onnx\n"
                "The service will refuse to start until hermes.onnx is present.",
                title="Model required",
            )
        )
    else:
        console.print(f"[green]Wake-word model present: {MODEL_PATH}[/green]")

    console.print("\n[bold]Next:[/bold] start with ./start.sh or:")
    console.print("  cd extras/wakeword-service && docker compose up --build -d")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes wake-word service setup")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults / existing values without prompting.",
    )
    args = parser.parse_args()
    configure(non_interactive=args.non_interactive or not sys.stdin.isatty())


if __name__ == "__main__":
    main()
