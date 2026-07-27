import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronicle_tray.sections import vault


def test_vault_environment_loads_from_repository_root(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://chronicle.example\n"
        "CHRONICLE_API_KEY=chrn_abc123_secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(vault, "REPO_ROOT", tmp_path)
    for name in ("BACKEND_URL", "CHRONICLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    vault._load_vault_environment()

    assert os.environ["BACKEND_URL"] == "https://chronicle.example"
    assert os.environ["CHRONICLE_API_KEY"] == "chrn_abc123_secret"
