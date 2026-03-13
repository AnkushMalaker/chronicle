#!/usr/bin/env python3
"""
Generate Docker Compose configuration files
"""

import os
import sys
from pathlib import Path

# Add lib directory to path
sys.path.append(str(Path(__file__).parent / "lib"))

from env_utils import format_variable, get_config_env_variables, get_skaffold_variables


def generate_env_file(service_name: str, output_path: str):
    """Generate a .env file for a service with only config.env variables."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Get list of variables defined in config.env
    config_variables = get_config_env_variables()

    # For skaffold, only include variables actually used by Skaffold templates
    if service_name == "skaffold":
        skaffold_needed_vars = get_skaffold_variables()
        config_variables = config_variables.intersection(skaffold_needed_vars)

    with open(output_path, "w") as f:
        f.write("# Auto-generated - DO NOT EDIT DIRECTLY\n")
        f.write("# Edit config.env and run 'make config' to regenerate\n")
        if service_name == "skaffold":
            f.write("# This file contains ONLY variables used by Skaffold templates\n")
        f.write("\n")

        # Only include variables that are defined in config.env
        for var_name in sorted(config_variables):
            var_value = os.environ.get(var_name)
            if var_value:  # Only include if variable has a value
                f.write(f"{format_variable(var_name, var_value, service_name)}\n")


def main():
    """Generate Docker Compose configuration files."""

    # Define output files
    outputs = {
        "advanced-backend": "backends/advanced/.env",
        "speaker-recognition": "extras/speaker-recognition/.env",
        "openmemory-mcp": "extras/openmemory-mcp/.env",
        "asr-services": "extras/asr-services/.env",
        "havpe-relay": "extras/havpe-relay/.env",
        "simple-backend": "backends/simple/.env",
        "omi-webhook-compatible": "backends/other-backends/omi-webhook-compatible/.env",
        "skaffold": "skaffold.env",
    }

    for service_name, output_path in outputs.items():
        print(f"Generating {service_name} configuration...")
        generate_env_file(service_name, output_path)
        print(f"✅ {service_name} configuration generated")


if __name__ == "__main__":
    main()
