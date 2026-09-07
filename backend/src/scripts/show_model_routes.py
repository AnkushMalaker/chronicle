#!/usr/bin/env python3
"""Print Chronicle's effective selected LLM and vision routes.

Run from ``backend``:

    uv run python src/scripts/show_model_routes.py
    uv run python src/scripts/show_model_routes.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from pathlib import Path

from backend.config_loader import load_config
from backend.model_routes import (
    effective_model_routes,
    effective_operation_routes,
    format_model_routes,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="audit every named LLM operation instead of only high-level routes",
    )
    args = parser.parse_args()
    warnings.filterwarnings(
        "ignore", message=r"In the sequence .*some elements are missing.*"
    )
    if "CONFIG_DIR" not in os.environ and not Path("/app/config/defaults.yml").exists():
        os.environ["CONFIG_DIR"] = str(Path(__file__).resolve().parents[3] / "config")
    config = load_config()
    routes = (
        effective_operation_routes() if args.all else effective_model_routes(config)
    )
    print(json.dumps(routes, indent=2) if args.json else format_model_routes(routes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
