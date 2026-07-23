"""Sibling-project path bootstrap.

The tray reuses the vault-sync and wearable-client code in place (same repo
checkout) rather than packaging them: both are flat-module uv projects, so
they're imported by putting their directories on sys.path.

Ordering matters: both projects have flat ``main``/``service`` modules. Only
the wearable client's are ever imported (via ble_manager), so its directory is
*inserted at the front* while the vault dir is *appended* — ``import main``
must always resolve to the wearable client's.
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]  # extras/chronicle-tray
EXTRAS_DIR = PROJECT_DIR.parent
REPO_ROOT = EXTRAS_DIR.parent

VAULT_SYNC_DIR = EXTRAS_DIR / "vault-sync"
WEARABLE_DIR = EXTRAS_DIR / "local-wearable-client"


def add_repo_root() -> None:
    """Make repo-root modules (clients.py, discovery.py) importable."""
    path = str(REPO_ROOT)
    if path not in sys.path:
        sys.path.append(path)


def add_vault_path() -> None:
    path = str(VAULT_SYNC_DIR)
    if path not in sys.path:
        sys.path.append(path)


def add_wearable_path() -> None:
    path = str(WEARABLE_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
