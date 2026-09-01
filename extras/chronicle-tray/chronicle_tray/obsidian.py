"""Small Obsidian desktop-vault registration seam for synced folders."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ObsidianVaultRegistration:
    vault_id: str
    added: bool


def obsidian_registry_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/obsidian/obsidian.json"
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        return Path(appdata) / "obsidian/obsidian.json" if appdata else Path()
    return Path.home() / ".config/obsidian/obsidian.json"


def register_obsidian_vault(
    vault_path: str,
    *,
    registry_path: Optional[Path] = None,
    timestamp_ms: Optional[int] = None,
) -> Optional[ObsidianVaultRegistration]:
    """Register a local folder using Obsidian's desktop vault registry.

    Obsidian's public URI and CLI can only open vaults already in this registry;
    neither exposes an add-vault operation. Missing/unreadable registries are left
    untouched so the normal URI launch remains a safe fallback.
    """

    registry = registry_path or obsidian_registry_path()
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    resolved = str(Path(vault_path).expanduser().resolve())
    vaults = data.setdefault("vaults", {})
    if not isinstance(vaults, dict):
        return None
    for vault_id, entry in vaults.items():
        if isinstance(entry, dict) and entry.get("path") == resolved:
            return ObsidianVaultRegistration(vault_id=vault_id, added=False)

    vault_id = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    suffix = 0
    while vault_id in vaults:
        suffix += 1
        vault_id = hashlib.sha256(f"{resolved}:{suffix}".encode("utf-8")).hexdigest()[
            :16
        ]
    vaults[vault_id] = {
        "path": resolved,
        "ts": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
    }

    backup = registry.with_name(f"{registry.name}.chronicle-backup")
    try:
        if not backup.exists():
            shutil.copy2(registry, backup)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{registry.name}.", dir=registry.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, registry.stat().st_mode)
            os.replace(temporary, registry)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        return None
    return ObsidianVaultRegistration(vault_id=vault_id, added=True)
