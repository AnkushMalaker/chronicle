"""
Chronicle Test Environment Setup Script.

Interactive configuration for test API keys (Deepgram, OpenAI).
Follows the same pattern as backends/advanced/init.py.
"""

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from setup_utils import is_placeholder, prompt_with_existing_masked

SETUP_DIR = Path(__file__).resolve().parent
ENV_TEST_PATH = SETUP_DIR / ".env.test"
ENV_TEST_TEMPLATE = SETUP_DIR / ".env.test.template"

DEEPGRAM_PLACEHOLDERS = ["your-deepgram-api-key-here", "your_deepgram_api_key_here"]
OPENAI_PLACEHOLDERS = ["your-openai-api-key-here", "your_openai_api_key_here"]


def main():
    parser = argparse.ArgumentParser(description="Chronicle Test Environment Setup")
    parser.add_argument(
        "--deepgram-api-key", help="Deepgram API key (skips interactive prompt)"
    )
    parser.add_argument(
        "--openai-api-key", help="OpenAI API key (skips interactive prompt)"
    )
    args = parser.parse_args()

    console = Console()

    console.print()
    panel = Panel(
        Text("Chronicle Test Environment Setup", style="cyan bold"),
        style="cyan",
        expand=False,
    )
    console.print(panel)
    console.print()

    # Ensure template exists
    if not ENV_TEST_TEMPLATE.exists():
        console.print(f"[red][ERROR][/red] Template not found: {ENV_TEST_TEMPLATE}")
        sys.exit(1)

    # Copy template to .env.test if it doesn't exist
    if not ENV_TEST_PATH.exists():
        shutil.copy2(ENV_TEST_TEMPLATE, ENV_TEST_PATH)
        console.print("[blue][INFO][/blue] Created .env.test from template")
    else:
        console.print("[blue][INFO][/blue] Found existing .env.test")

    env_path_str = str(ENV_TEST_PATH)

    # --- Deepgram API Key ---
    if args.deepgram_api_key:
        deepgram_key = args.deepgram_api_key
        console.print("[green][OK][/green] Deepgram API key provided via argument")
    else:
        deepgram_key = prompt_with_existing_masked(
            prompt_text="Deepgram API key",
            env_file_path=env_path_str,
            env_key="DEEPGRAM_API_KEY",
            placeholders=DEEPGRAM_PLACEHOLDERS,
            is_password=True,
        )

    if deepgram_key and not is_placeholder(deepgram_key, *DEEPGRAM_PLACEHOLDERS):
        set_key(env_path_str, "DEEPGRAM_API_KEY", deepgram_key)
        console.print("[green][OK][/green] Deepgram API key saved")
    else:
        console.print(
            "[yellow][WARNING][/yellow] No Deepgram key configured - "
            "tests tagged requires-api-keys will fail"
        )

    # --- OpenAI API Key ---
    if args.openai_api_key:
        openai_key = args.openai_api_key
        console.print("[green][OK][/green] OpenAI API key provided via argument")
    else:
        openai_key = prompt_with_existing_masked(
            prompt_text="OpenAI API key",
            env_file_path=env_path_str,
            env_key="OPENAI_API_KEY",
            placeholders=OPENAI_PLACEHOLDERS,
            is_password=True,
        )

    if openai_key and not is_placeholder(openai_key, *OPENAI_PLACEHOLDERS):
        set_key(env_path_str, "OPENAI_API_KEY", openai_key)
        console.print("[green][OK][/green] OpenAI API key saved")
    else:
        console.print(
            "[yellow][WARNING][/yellow] No OpenAI key configured - "
            "tests tagged requires-api-keys will fail"
        )

    console.print()
    console.print("[green][DONE][/green] Test environment configured")
    console.print(f"  Config file: {ENV_TEST_PATH}")
    console.print()
    console.print("Next steps:")
    console.print("  [cyan]make start[/cyan]   - Start test containers")
    console.print("  [cyan]make test[/cyan]    - Start containers + run tests")


if __name__ == "__main__":
    main()
