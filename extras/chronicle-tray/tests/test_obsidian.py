import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronicle_tray.obsidian import register_obsidian_vault


def test_register_obsidian_vault_is_atomic_and_idempotent(tmp_path):
    registry = tmp_path / "obsidian.json"
    registry.write_text(
        json.dumps({"vaults": {"main-id": {"path": str(tmp_path / "Main")}}}),
        encoding="utf-8",
    )
    space = tmp_path / "Chronicle Spaces" / "Brainstorm"
    space.mkdir(parents=True)

    first = register_obsidian_vault(
        str(space), registry_path=registry, timestamp_ms=1234
    )
    second = register_obsidian_vault(
        str(space), registry_path=registry, timestamp_ms=5678
    )

    assert first is not None and first.added
    assert second is not None and not second.added
    assert second.vault_id == first.vault_id
    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert saved["vaults"][first.vault_id] == {
        "path": str(space.resolve()),
        "ts": 1234,
    }
    assert saved["vaults"]["main-id"]["path"] == str(tmp_path / "Main")
    assert registry.with_name("obsidian.json.chronicle-backup").is_file()


def test_register_obsidian_vault_leaves_invalid_registry_untouched(tmp_path):
    registry = tmp_path / "obsidian.json"
    registry.write_text("not json", encoding="utf-8")

    assert (
        register_obsidian_vault(str(tmp_path / "Space"), registry_path=registry) is None
    )
    assert registry.read_text(encoding="utf-8") == "not json"
