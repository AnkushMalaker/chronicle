import shutil

import pytest

from backend.services.memory.agent.vault_tools import VaultTools


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_vault_search_does_not_inherit_git_ignore_rules(tmp_path):
    output = tmp_path / "private-run"
    output.mkdir()
    (output / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    vault = output / "vault"
    template = vault / "Templates" / "Person Template.md"
    template.parent.mkdir(parents=True)
    template.write_text("# Person Template\n\nCanonical marker.\n", encoding="utf-8")
    tools = VaultTools(vault)

    assert tools.glob("Templates/*.md") == "Templates/Person Template.md"
    assert tools.grep("Canonical marker") == "Templates/Person Template.md"
