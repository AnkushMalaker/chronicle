"""Deterministic person-merge behavior shared by API, Obsidian, and agents."""

import contextlib
import hashlib

import pytest
from ruamel.yaml import YAML

from advanced_omi_backend.services.memory import person_merge, person_merge_actions
from advanced_omi_backend.services.memory.audit import (
    MemoryCause,
    actor_for,
    source_label_for,
)
from advanced_omi_backend.services.memory.person_merge import (
    PersonMergeService,
    PersonMergeStale,
)


def _person(
    name: str,
    about: list[str],
    mentions: list[str],
    *,
    aliases: list[str] | None = None,
    distinct_from: list[str] | None = None,
    org: str = "",
    role: str = "",
    photo: str = "",
) -> str:
    alias_lines = "\n".join(f"  - {alias}" for alias in aliases or []) or "[]"
    if aliases:
        alias_value = f"\n{alias_lines}"
    else:
        alias_value = " []"
    distinct_lines = (
        "\n" + "\n".join(f'  - "[[{name}]]"' for name in distinct_from)
        if distinct_from
        else " []"
    )
    image = f"![[../_media/{photo}|200]]\n" if photo else ""
    return (
        "---\n"
        'categories:\n  - "[[People]]"\n'
        f"aliases:{alias_value}\n"
        f"distinct_from:{distinct_lines}\n"
        f"org: {org}\n"
        f"role: {role}\n"
        "relationship:\nlocation:\ncreated: 2026-07-28\nupdated: 2026-07-28\n"
        "---\n"
        f"{image}"
        "## About\n"
        + "\n".join(f"- {fact}" for fact in about)
        + "\n\n## Conversations\n![[Conversations.base#Person]]\n\n## Mentions\n"
        + "\n".join(f"- {mention}" for mention in mentions)
        + "\n"
    )


def _frontmatter(text: str) -> dict:
    end = text.index("\n---\n", 4)
    return YAML(typ="safe").load(text[4:end])


@pytest.fixture
def vault(tmp_path):
    people = tmp_path / "People"
    conversations = tmp_path / "Conversations"
    people.mkdir()
    conversations.mkdir()
    (people / "Amay.md").write_text(
        _person(
            "Amay",
            ["Owns the radar pipeline.", "Shared fact."],
            ["2026-07-28 — Planned work."],
            aliases=["A. May"],
            distinct_from=["Carol"],
            org="Acme",
            role="Engineer",
            photo="amay.jpg",
        ),
        encoding="utf-8",
    )
    (people / "Amey.md").write_text(
        _person(
            "Amey",
            ["Discussed model parsing.", "Shared fact."],
            ["2026-07-28 — Discussed metrics."],
            aliases=["A Mehta"],
            org="Acme",
            role="Lead",
            photo="amey.jpg",
        ),
        encoding="utf-8",
    )
    (conversations / "one.md").write_text(
        'people:\n  - "[[Amay]]"\n- [[Amay]] owns this.\n', encoding="utf-8"
    )
    (conversations / "two.md").write_text(
        "See [[People/Amay|Amay from work]].\n", encoding="utf-8"
    )
    return tmp_path


def test_preview_is_read_only_and_reports_complete_plan(vault):
    service = PersonMergeService(vault)
    source_before = (vault / "People/Amay.md").read_text(encoding="utf-8")

    preview = service.preview("amay", "AMEY")

    assert preview.source_name == "Amay"
    assert preview.target_name == "Amey"
    assert preview.facts_to_add == 2
    assert preview.duplicate_facts_skipped == 1
    assert preview.backlink_files == ["Conversations/one.md", "Conversations/two.md"]
    assert preview.backlink_occurrences == 3
    assert [conflict.field for conflict in preview.metadata_conflicts] == ["role"]
    assert (vault / "People/Amay.md").read_text(encoding="utf-8") == source_before


def test_apply_merges_metadata_facts_media_and_backlinks(vault, monkeypatch):
    monkeypatch.setattr(
        person_merge, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonMergeService(vault)
    preview = service.preview("Amay", "Amey")

    result = service.apply("Amay", "Amey", preview.plan_token)

    assert not (vault / "People/Amay.md").exists()
    merged = (vault / "People/Amey.md").read_text(encoding="utf-8")
    metadata = _frontmatter(merged)
    assert metadata["aliases"] == ["A Mehta", "A. May", "Amay"]
    assert metadata["distinct_from"] == ["[[Carol]]"]
    assert metadata["org"] == "Acme"
    assert metadata["role"] == "Lead"
    assert "Owns the radar pipeline" in merged
    assert merged.count("Shared fact") == 1
    assert "amey.jpg" in merged and "amay.jpg" in merged
    assert "[[Amay]]" not in (vault / "Conversations/one.md").read_text(
        encoding="utf-8"
    )
    assert "[[People/Amey|Amay from work]]" in (
        vault / "Conversations/two.md"
    ).read_text(encoding="utf-8")
    assert set(result.changed_paths) == {
        "People/Amay.md",
        "People/Amey.md",
        "Conversations/one.md",
        "Conversations/two.md",
    }


def test_apply_rejects_a_stale_preview(vault, monkeypatch):
    monkeypatch.setattr(
        person_merge, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonMergeService(vault)
    preview = service.preview("Amay", "Amey")
    target = vault / "People/Amey.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nNew edit.\n", encoding="utf-8"
    )

    with pytest.raises(PersonMergeStale, match="Preview it again"):
        service.apply("Amay", "Amey", preview.plan_token)


def test_preview_rejects_a_local_copy_that_is_not_synced(vault):
    local_hash = hashlib.sha256(b"older local note").hexdigest()
    with pytest.raises(PersonMergeStale, match="source note differs"):
        PersonMergeService(vault).preview(
            "Amay", "Amey", expected_source_hash=local_hash
        )


def test_apply_rolls_back_files_when_a_write_fails(vault, monkeypatch):
    monkeypatch.setattr(
        person_merge, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonMergeService(vault)
    preview = service.preview("Amay", "Amey")
    before = {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    }
    real_write = person_merge._atomic_write
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if not failed and path.name == "one.md":
            failed = True
            raise OSError("simulated write failure")
        real_write(path, content)

    monkeypatch.setattr(person_merge, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="simulated"):
        service.apply("Amay", "Amey", preview.plan_token)

    after = {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    }
    assert after == before


async def test_merge_audit_covers_every_changed_note(vault, monkeypatch):
    monkeypatch.setattr(
        person_merge, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonMergeService(vault)
    preview = service.preview("Amay", "Amey")
    result = service.apply("Amay", "Amey", preview.plan_token)
    entries = []

    async def capture(**kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(person_merge_actions, "record_vault_change", capture)
    await person_merge_actions._record_merge_audit("user-1", result)

    assert {entry["note_path"] for entry in entries} == set(result.changed_paths)
    assert {entry["action_id"] for entry in entries} == {result.action_id}
    source = next(entry for entry in entries if entry["note_path"] == "People/Amay.md")
    assert source["operation"] == "rename"
    assert source["after"] is None
    assert source["new_path"] == "People/Amey.md"


def test_obsidian_action_provenance_is_human():
    assert source_label_for(MemoryCause.OBSIDIAN_ACTION, False, "update") == (
        "Obsidian action"
    )
    assert actor_for(MemoryCause.OBSIDIAN_ACTION, False, "update") == "human_external"
