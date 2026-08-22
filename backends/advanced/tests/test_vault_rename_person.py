"""Unit tests for ``VaultTools.rename_person``.

Regression cover for the audit blind spot and the lossy merge that made a renamed
note vanish from the ledger (it re-appeared later as an unexplained ``create``):

- a rename/merge must be *recorded* in ``VaultTools.removed`` so the provider can
  emit a ``rename`` audit-ledger entry, and
- a merge must migrate the retiring note's facts into the target *before* deleting
  it — never rely on a follow-up ``edit_note`` that may never come.
"""

import pytest

from advanced_omi_backend.services.memory.agent.vault_tools import (
    VaultToolError,
    VaultTools,
)

# Every mutating VaultTools call takes the Redis-backed per-note write lock, which
# fails closed on purpose — so these need a real Redis even though the vault itself
# is a tmp_path.
pytestmark = pytest.mark.usefixtures("redis_service")

PERSON_TEMPLATE = """---
categories:
  - "[[People]]"
created: 2026-06-13
updated: 2026-06-15
---
## About
{about}

## Conversations
![[Conversations.base#Person]]

## Mentions
{mentions}
"""


def _person(about: str, mentions: str) -> str:
    return PERSON_TEMPLATE.format(about=about, mentions=mentions)


def test_unknown_speaker_person_note_is_rejected(tmp_path):
    tools = VaultTools(tmp_path)
    with pytest.raises(VaultToolError, match="diarization placeholders"):
        tools.write_note("People/Unknown Speaker 4.md", _person("- x", "- y"))


def test_hermes_person_note_is_rejected(tmp_path):
    tools = VaultTools(tmp_path)
    with pytest.raises(VaultToolError, match="Hermes assistant"):
        tools.write_note("People/Hermes.md", _person("- x", "- y"))


class TestRenamePersonMerge:
    def test_merge_migrates_facts_before_deleting_old(self, tmp_path):
        tools = VaultTools(tmp_path)
        (tmp_path / "People").mkdir(parents=True, exist_ok=True)
        (tmp_path / "People" / "kenneth.md").write_text(
            _person(
                about="- Talked about API usage.",
                mentions="- 2026-06-13 — commented on non-technical people.",
            ),
            encoding="utf-8",
        )
        (tmp_path / "People" / "Naren.md").write_text(
            _person(about="- Interested in agents.", mentions="- 2026-06-29 — spoke."),
            encoding="utf-8",
        )

        msg = tools.rename_person("kenneth", "Naren")

        # Old note is gone; target absorbed the retiring note's facts.
        assert not (tmp_path / "People" / "kenneth.md").exists()
        naren = (tmp_path / "People" / "Naren.md").read_text(encoding="utf-8")
        assert "API usage" in naren
        assert "non-technical people" in naren
        # No facts were dropped and no orphan section was needed (both headings exist).
        assert "## Merged from" not in naren
        assert "migrated 2 fact bullet(s)" in msg

    def test_merge_records_removal_for_audit(self, tmp_path):
        tools = VaultTools(tmp_path)
        (tmp_path / "People").mkdir(parents=True, exist_ok=True)
        (tmp_path / "People" / "kenneth.md").write_text(
            _person(about="- x.", mentions="- y."), encoding="utf-8"
        )
        (tmp_path / "People" / "Naren.md").write_text(
            _person(about="- a.", mentions="- b."), encoding="utf-8"
        )

        tools.rename_person("kenneth", "Naren")

        assert len(tools.removed) == 1
        rec = tools.removed[0]
        assert rec["old_path"] == "People/kenneth.md"
        assert rec["new_path"] == "People/Naren.md"
        assert "- x." in rec["before"]  # pre-removal content captured for the ledger
        # The vanished path must not linger in `touched` (nothing to re-read there).
        assert "People/kenneth.md" not in tools.touched
        assert "People/Naren.md" in tools.touched


class TestRenamePersonMove:
    def test_case_only_repeat_is_an_idempotent_noop(self, tmp_path):
        """The agent may request title casing after case-insensitive path resolution."""

        tools = VaultTools(tmp_path)
        people = tmp_path / "People"
        people.mkdir(parents=True, exist_ok=True)
        source = people / "blair.md"
        original = _person(about="- fact.", mentions="- mention.")
        source.write_text(original, encoding="utf-8")

        message = tools.rename_person("blair", "Blair")

        assert "already resolves" in message
        assert source.read_text(encoding="utf-8") == original
        assert not (people / "Blair.md").exists()
        assert tools.touched == set()
        assert tools.removed == []

    def test_plain_rename_records_removal(self, tmp_path):
        tools = VaultTools(tmp_path)
        (tmp_path / "People").mkdir(parents=True, exist_ok=True)
        (tmp_path / "People" / "kenneth.md").write_text(
            _person(about="- fact.", mentions="- m."), encoding="utf-8"
        )

        tools.rename_person("kenneth", "Kenneth Long")

        assert not (tmp_path / "People" / "kenneth.md").exists()
        assert (tmp_path / "People" / "Kenneth Long.md").exists()
        assert [r["old_path"] for r in tools.removed] == ["People/kenneth.md"]
        assert tools.removed[0]["new_path"] == "People/Kenneth Long.md"
        assert "People/kenneth.md" not in tools.touched

    def test_cli_rename_audits_every_rewritten_backlink(self, tmp_path, monkeypatch):
        tools = VaultTools(tmp_path)
        people = tmp_path / "People"
        people.mkdir(parents=True, exist_ok=True)
        source = people / "kenneth.md"
        source.write_text(_person(about="- fact.", mentions="- m."), encoding="utf-8")
        conversations = tmp_path / "Conversations"
        conversations.mkdir()
        backlink = conversations / "conversation-1.md"
        backlink.write_text("Spoke with [[kenneth]].", encoding="utf-8")
        tools._notesmd = "/usr/bin/fake-notesmd"

        def fake_move(old_rel, new_rel):
            source.rename(tmp_path / new_rel)
            backlink.write_text("Spoke with [[Kenneth Long]].", encoding="utf-8")

        monkeypatch.setattr(tools, "_move_cli", fake_move)

        tools.rename_person("kenneth", "Kenneth Long")

        assert tools.touched == {
            "People/Kenneth Long.md",
            "Conversations/conversation-1.md",
        }
        assert tools.removed[0]["old_path"] == "People/kenneth.md"

    def test_failed_cli_rewrite_is_audited_before_python_fallback(
        self, tmp_path, monkeypatch
    ):
        tools = VaultTools(tmp_path)
        people = tmp_path / "People"
        people.mkdir(parents=True, exist_ok=True)
        source = people / "kenneth.md"
        source.write_text(_person(about="- fact.", mentions="- m."), encoding="utf-8")
        conversations = tmp_path / "Conversations"
        conversations.mkdir()
        backlink = conversations / "conversation-1.md"
        backlink.write_text("Spoke with [[kenneth]].", encoding="utf-8")
        tools._notesmd = "/usr/bin/fake-notesmd"

        def fake_failed_move(old_rel, new_rel):
            backlink.write_text("Spoke with [[Kenneth Long]].", encoding="utf-8")
            raise RuntimeError("notesmd exited non-zero after rewriting a backlink")

        monkeypatch.setattr(tools, "_move_cli", fake_failed_move)

        tools.rename_person("kenneth", "Kenneth Long")

        assert tools.touched == {
            "People/Kenneth Long.md",
            "Conversations/conversation-1.md",
        }
        assert tools.removed[0]["old_path"] == "People/kenneth.md"
