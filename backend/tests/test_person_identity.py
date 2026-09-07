"""Duplicate suggestions and durable distinct-person identity decisions."""

import contextlib

import pytest
from ruamel.yaml import YAML

from backend.services.memory import person_identity, person_merge_actions
from backend.services.memory.person_identity import PersonIdentityService
from backend.services.memory.person_merge import (
    PersonMergeError,
    PersonMergeService,
    PersonMergeStale,
)


def _person(
    name: str,
    *,
    aliases: list[str] | None = None,
    distinct_from: list[str] | None = None,
    org: str = "",
    topic: str = "",
    conversation: str = "",
    photo: str = "",
) -> str:
    aliases_yaml = (
        "\n" + "\n".join(f"  - {value}" for value in aliases) if aliases else " []"
    )
    distinct_yaml = (
        "\n" + "\n".join(f'  - "[[{value}]]"' for value in distinct_from)
        if distinct_from
        else " []"
    )
    image = f"![[../_media/{photo}|200]]\n" if photo else ""
    context = f" Discussed [[{topic}]]." if topic else ""
    mention = (
        f"- Met in [[Conversations/{conversation}|Conversation]].\n"
        if conversation
        else "- Mentioned once.\n"
    )
    return (
        "---\n"
        'categories:\n  - "[[People]]"\n'
        f"aliases:{aliases_yaml}\n"
        f"distinct_from:{distinct_yaml}\n"
        f"org: {org}\n"
        "role:\nrelationship:\nlocation:\n"
        "created: 2026-08-01\nupdated: 2026-08-01\n"
        "---\n"
        f"{image}"
        "## About\n"
        f"- Information about {name}.{context}\n\n"
        "## Conversations\n![[Conversations.base#Person]]\n\n"
        "## Mentions\n"
        f"{mention}"
    )


def _metadata(path) -> dict:
    text = path.read_text(encoding="utf-8")
    end = text.index("\n---\n", 4)
    return YAML(typ="safe").load(text[4:end])


@pytest.fixture
def vault(tmp_path):
    people = tmp_path / "People"
    people.mkdir()
    (people / "Sabi.md").write_text(
        _person(
            "Sabi",
            org="Acme",
            topic="Model Training",
            conversation="11111111-1111-1111-1111-111111111111",
        ),
        encoding="utf-8",
    )
    (people / "Sabri.md").write_text(
        _person(
            "Sabri",
            org="Acme",
            topic="Model Training",
            conversation="11111111-1111-1111-1111-111111111111",
        ),
        encoding="utf-8",
    )
    (people / "Robert.md").write_text(
        _person("Robert", aliases=["Bob"]), encoding="utf-8"
    )
    (people / "Bob.md").write_text(_person("Bob"), encoding="utf-8")
    (people / "Alice.md").write_text(_person("Alice"), encoding="utf-8")
    (people / "Carlos.md").write_text(
        _person(
            "Carlos",
            topic="Shared Project",
            conversation="22222222-2222-2222-2222-222222222222",
        ),
        encoding="utf-8",
    )
    (people / "Diana.md").write_text(
        _person(
            "Diana",
            topic="Shared Project",
            conversation="22222222-2222-2222-2222-222222222222",
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_suggestions_combine_name_alias_and_context_evidence(vault):
    suggestions = PersonIdentityService(vault).suggestions()
    by_pair = {
        frozenset((item["person_a"]["name"], item["person_b"]["name"])): item
        for item in suggestions
    }

    sabi = by_pair[frozenset(("Sabi", "Sabri"))]
    assert sabi["score"] >= 100
    assert "names differ by one character" in sabi["reasons"]
    assert "same organization" in sabi["reasons"]
    assert any(
        reason.startswith("same source conversation") for reason in sabi["reasons"]
    )
    assert sabi["revision"]

    robert = by_pair[frozenset(("Robert", "Bob"))]
    assert "one name is already an alias of the other" in robert["reasons"]
    assert frozenset(("Alice", "Bob")) not in by_pair
    assert frozenset(("Carlos", "Diana")) not in by_pair


def test_distinct_decision_is_symmetric_and_removes_suggestion(vault, monkeypatch):
    monkeypatch.setattr(
        person_identity, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonIdentityService(vault)
    suggestion = next(
        item
        for item in service.suggestions()
        if {item["person_a"]["name"], item["person_b"]["name"]} == {"Sabi", "Sabri"}
    )

    result = service.set_distinct(
        "Sabi", "Sabri", distinct=True, revision=suggestion["revision"]
    )

    assert result.decision == "distinct"
    assert set(result.changed_paths) == {"People/Sabi.md", "People/Sabri.md"}
    assert _metadata(vault / "People/Sabi.md")["distinct_from"] == ["[[Sabri]]"]
    assert _metadata(vault / "People/Sabri.md")["distinct_from"] == ["[[Sabi]]"]
    assert not any(
        {item["person_a"]["name"], item["person_b"]["name"]} == {"Sabi", "Sabri"}
        for item in service.suggestions()
    )


def test_distinct_decision_blocks_merge_until_cleared(vault, monkeypatch):
    monkeypatch.setattr(
        person_identity, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    identity = PersonIdentityService(vault)
    identity.set_distinct("Sabi", "Sabri", distinct=True)

    with pytest.raises(PersonMergeError, match="marked as separate people"):
        PersonMergeService(vault).preview("Sabi", "Sabri")

    identity.set_distinct("Sabi", "Sabri", distinct=False)
    preview = PersonMergeService(vault).preview("Sabi", "Sabri")
    assert preview.source_name == "Sabi"


def test_distinct_decision_rejects_stale_suggestion(vault, monkeypatch):
    monkeypatch.setattr(
        person_identity, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    service = PersonIdentityService(vault)
    suggestion = next(
        item
        for item in service.suggestions()
        if {item["person_a"]["name"], item["person_b"]["name"]} == {"Sabi", "Sabri"}
    )
    path = vault / "People/Sabi.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    with pytest.raises(PersonMergeStale, match="changed after the suggestion"):
        service.set_distinct(
            "Sabi", "Sabri", distinct=True, revision=suggestion["revision"]
        )


def test_existing_one_sided_annotation_is_respected(vault):
    path = vault / "People/Sabi.md"
    path.write_text(
        _person("Sabi", distinct_from=["Sabri"]),
        encoding="utf-8",
    )
    suggestions = PersonIdentityService(vault).suggestions()
    assert not any(
        {item["person_a"]["name"], item["person_b"]["name"]} == {"Sabi", "Sabri"}
        for item in suggestions
    )
    with pytest.raises(PersonMergeError, match="marked as separate people"):
        PersonMergeService(vault).preview("Sabi", "Sabri")


async def test_identity_decision_audits_both_notes(vault, monkeypatch):
    monkeypatch.setattr(
        person_identity, "vault_note_lock", lambda _user: contextlib.nullcontext()
    )
    result = PersonIdentityService(vault).set_distinct("Sabi", "Sabri", distinct=True)
    entries = []

    async def capture(**kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(person_merge_actions, "record_vault_change", capture)
    await person_merge_actions._record_identity_audit("user-1", result)

    assert {entry["note_path"] for entry in entries} == {
        "People/Sabi.md",
        "People/Sabri.md",
    }
    assert {entry["identity_decision"] for entry in entries} == {"distinct"}
    assert {entry["action_id"] for entry in entries} == {result.action_id}
