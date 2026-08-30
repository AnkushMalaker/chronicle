"""Chronicle vault sync — pair this machine's Syncthing with the server's vault.

The menu-bar UI lives in ``extras/chronicle-tray``; it imports this package as a
dependency rather than by path injection.
"""

from chronicle_vault_sync.core import (
    VaultSyncConfig,
    broker_folders,
    broker_pair,
    broker_space_action,
    persisted_vault_dir,
    save_vault_dir,
)
from chronicle_vault_sync.syncthing import SyncthingManager

__all__ = [
    "SyncthingManager",
    "VaultSyncConfig",
    "broker_pair",
    "broker_folders",
    "broker_space_action",
    "persisted_vault_dir",
    "save_vault_dir",
]
