import pytest

from advanced_omi_backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from advanced_omi_backend.services.memory_spaces import (
    MemorySpaceService,
    _merge_created_note,
)

pytestmark = pytest.mark.unit

SPACE_ID = "9f3523c8-af75-469d-995a-7179531f3fc8"


def test_scope_keeps_main_and_space_as_sibling_vaults(tmp_path):
    resolver = MemoryScopeResolver(tmp_path)

    main = resolver.vault_root(MemoryScope("user-1"))
    space = resolver.vault_root(MemoryScope("user-1", SPACE_ID))

    assert main == tmp_path / "conversation_docs" / "user-1"
    assert space == tmp_path / "memory_spaces" / "user-1" / SPACE_ID / "vault"
    assert main not in space.parents
    assert space not in main.parents


def test_blank_space_note_collision_preserves_main_and_adds_workspace_facts():
    main = """---
categories: [\"[[Topics]]\"]
created: 2026-08-01
---
# Project Atlas

## About

- Main owns the existing launch plan.
"""
    workspace = """---
categories: [\"[[Topics]]\"]
created: 2026-08-29
---
# Project Atlas

## About

- The brainstorm proposes a paper prototype.

## Open questions

- Who will test it?
"""

    merged = _merge_created_note(main, workspace)

    assert "created: 2026-08-01" in merged
    assert "created: 2026-08-29" not in merged
    assert merged.count("# Project Atlas") == 1
    assert "- Main owns the existing launch plan." in merged
    assert "- The brainstorm proposes a paper prototype." in merged
    assert "## Open questions\n\n- Who will test it?" in merged


@pytest.mark.parametrize(
    "space_id",
    ["../escape", "not-a-uuid", "", "/absolute"],
)
def test_scope_rejects_unsafe_space_ids(tmp_path, space_id):
    resolver = MemoryScopeResolver(tmp_path)
    with pytest.raises(MemoryScopeError):
        resolver.vault_root(MemoryScope("user-1", space_id))


async def test_seed_preview_is_first_hop_only_and_copy_requires_confirmation(tmp_path):
    resolver = MemoryScopeResolver(tmp_path)
    service = MemorySpaceService(resolver)
    main = resolver.main_root("user-1")
    (main / "Topics").mkdir(parents=True)
    (main / "People").mkdir(parents=True)
    (main / "_media").mkdir(parents=True)
    (main / "Topics" / "Idea.md").write_text(
        "# Idea\n\n[[People/Ada]]\n\n![](_media/sketch.png)\n",
        encoding="utf-8",
    )
    (main / "People" / "Ada.md").write_text(
        "# Ada\n\n[[Topics/Transitive]]\n", encoding="utf-8"
    )
    (main / "Topics" / "Transitive.md").write_text("# Not copied\n", encoding="utf-8")
    (main / "_media" / "sketch.png").write_bytes(b"png")

    preview = await service.preview_seed("user-1", ["Topics/Idea.md"])
    assert [item["note_path"] for item in preview["suggestions"]] == ["People/Ada.md"]

    scope = MemoryScope("user-1", SPACE_ID)
    copied = service._copy_seed_sync(scope, ["Topics/Idea.md"])
    vault = resolver.vault_root(scope)

    assert [item.note_path for item in copied] == ["Topics/Idea.md"]
    assert (vault / "Topics" / "Idea.md").is_file()
    assert (vault / "_media" / "sketch.png").read_bytes() == b"png"
    assert not (vault / "People" / "Ada.md").exists()
    assert not (vault / "Topics" / "Transitive.md").exists()
    assert (resolver.baseline_root(scope) / "Topics" / "Idea.md").is_file()
