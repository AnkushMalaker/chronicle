import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chronicle_client.config as client_config
from chronicle_tray.sections import vault


def test_vault_environment_loads_from_repository_root(tmp_path, monkeypatch):
    """The tray reads its client config from the repository-root .env.

    That file is shared by every native client component (tray, vault sync,
    wearable client), so the tray must not carry its own copy of the location.
    """
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://chronicle.example\n"
        "CHRONICLE_API_KEY=chrn_abc123_secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client_config, "REPO_ROOT", tmp_path)
    # The loader is memoised so repeated section startups don't re-read the file.
    monkeypatch.setattr(client_config, "_env_loaded", False)
    for name in ("BACKEND_URL", "CHRONICLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    vault._load_vault_environment()

    assert os.environ["BACKEND_URL"] == "https://chronicle.example"
    assert os.environ["CHRONICLE_API_KEY"] == "chrn_abc123_secret"


def test_vault_environment_is_loaded_only_once(tmp_path, monkeypatch):
    """Re-entry must not re-read the file — several tray sections call this."""
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://first.example\n", encoding="utf-8"
    )
    monkeypatch.setattr(client_config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(client_config, "_env_loaded", False)
    monkeypatch.delenv("BACKEND_URL", raising=False)

    vault._load_vault_environment()
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://second.example\n", encoding="utf-8"
    )
    vault._load_vault_environment()

    assert os.environ["BACKEND_URL"] == "https://first.example"


def test_space_vault_defaults_to_a_sibling_of_main(tmp_path):
    manager = object.__new__(vault.VaultSyncManager)
    manager.config = type("Config", (), {"local_vault_dir": str(tmp_path / "Main")})()
    space_id = "9f3523c8-af75-469d-995a-7179531f3fc8"

    local_dir = Path(
        manager._local_dir({"memory_space_id": space_id, "name": "Launch brainstorm"})
    )

    assert local_dir.parent == tmp_path / "Chronicle Spaces"
    assert local_dir != tmp_path / "Main"
    assert space_id[:8] in local_dir.name
