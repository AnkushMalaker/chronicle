"""Unit tests for the Kepano-style vault scaffold.

Covers the spine layout (templates under Templates/, bases under Templates/Bases/, hubs at
root), the enumeration guard that keeps scaffolding out of captured memories, and organic
category creation.
"""

from backend.services.memory.vault_scaffold import (
    BASES_DIR,
    TEMPLATES_DIR,
    build_category_files,
    is_scaffold_note,
    seed_vault_scaffold,
    write_category,
)
from backend.services.memory.vault_templates import SPINE_TEMPLATES


class TestSeedVaultScaffold:
    def test_spine_layout(self, tmp_path):
        created = set(seed_vault_scaffold(tmp_path))
        # Templates live under Templates/, bases under Templates/Bases/, hubs at root.
        for name in SPINE_TEMPLATES:
            assert f"{TEMPLATES_DIR}/{name}" in created
        for base in ("People.base", "Conversations.base", "Topics.base"):
            assert f"{BASES_DIR}/{base}" in created
            assert (tmp_path / BASES_DIR / base).exists()
        for hub in ("People.md", "Conversations.md", "Topics.md"):
            assert hub in created
            assert (tmp_path / hub).exists()

    def test_idempotent(self, tmp_path):
        seed_vault_scaffold(tmp_path)
        # A user edit must not be clobbered on re-seed.
        hub = tmp_path / "People.md"
        hub.write_text("my own notes", encoding="utf-8")
        assert seed_vault_scaffold(tmp_path) == []
        assert hub.read_text(encoding="utf-8") == "my own notes"

    def test_person_template_embeds_base_view(self, tmp_path):
        seed_vault_scaffold(tmp_path)
        person = (tmp_path / TEMPLATES_DIR / "Person Template.md").read_text(
            encoding="utf-8"
        )
        assert "![[Conversations.base#Person]]" in person
        assert 'categories:\n  - "[[People]]"' in person


class TestIsScaffoldNote:
    def test_excludes_templates_and_hubs_keeps_real_notes(self, tmp_path):
        seed_vault_scaffold(tmp_path)
        (tmp_path / "People").mkdir(exist_ok=True)
        real = tmp_path / "People" / "Alice.md"
        real.write_text("x", encoding="utf-8")

        assert is_scaffold_note(tmp_path / "People.md", tmp_path) is True
        assert (
            is_scaffold_note(tmp_path / TEMPLATES_DIR / "Person Template.md", tmp_path)
            is True
        )
        assert is_scaffold_note(real, tmp_path) is False

        enumerated = [
            p.relative_to(tmp_path).as_posix()
            for p in tmp_path.rglob("*.md")
            if not is_scaffold_note(p, tmp_path)
        ]
        assert enumerated == ["People/Alice.md"]


class TestOrganicCategory:
    def test_build_category_files_shape(self):
        files = build_category_files("Places", ["location", "type"])
        assert set(files) == {
            f"{TEMPLATES_DIR}/Places Template.md",
            f"{BASES_DIR}/Places.base",
            "Places.md",
        }
        template = files[f"{TEMPLATES_DIR}/Places Template.md"]
        assert 'categories:\n  - "[[Places]]"' in template
        assert "location:" in template and "type:" in template
        assert (
            'categories.contains(link("Places"))' in files[f"{BASES_DIR}/Places.base"]
        )

    def test_invalid_property_names_dropped(self):
        # Only lowercase identifier-style keys are emitted (no spaces/uppercase/wikilinks).
        template = build_category_files("Books", ["author", "Bad Key", "[[x]]"])[
            f"{TEMPLATES_DIR}/Books Template.md"
        ]
        assert "author:" in template
        assert "Bad Key" not in template and "[[x]]" not in template

    def test_write_category_idempotent(self, tmp_path):
        first = write_category(tmp_path, "Projects", ["status"])
        assert f"{TEMPLATES_DIR}/Projects Template.md" in first
        assert write_category(tmp_path, "Projects", ["status"]) == []
