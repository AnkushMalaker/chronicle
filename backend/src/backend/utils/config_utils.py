"""Configuration utility functions."""

import os
from typing import Union


def resolve_value(value: Union[str, int, float]) -> Union[str, int, float]:
    """Resolve environment variable references in configuration values.

    Supports ${VAR} and ${VAR:-default} syntax. Returns the original value
    if it's not a string or doesn't match the pattern.
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        content = value[2:-1]
        if ":-" in content:
            var_name, default_val = content.split(":-", 1)
            return os.getenv(var_name, default_val)
        else:
            return os.getenv(content, "")
    return value
