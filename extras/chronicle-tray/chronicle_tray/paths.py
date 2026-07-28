"""Repo-root module bootstrap.

``clients.py`` and ``discovery.py`` still live at the repository root rather than
in a package, so they have to be reached via ``sys.path``. Everything else the
tray uses — vault sync, the wearable client, shared client config — is a real
dependency declared in ``pyproject.toml`` and imported normally.
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]  # extras/chronicle-tray
EXTRAS_DIR = PROJECT_DIR.parent
REPO_ROOT = EXTRAS_DIR.parent


def add_repo_root() -> None:
    """Make repo-root modules (clients.py, discovery.py) importable."""
    path = str(REPO_ROOT)
    if path not in sys.path:
        sys.path.append(path)
