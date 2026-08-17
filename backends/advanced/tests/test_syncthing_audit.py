import pytest

from advanced_omi_backend.services.memory import syncthing_audit


@pytest.mark.asyncio
async def test_inbound_root_content_note_is_audited(tmp_path, monkeypatch):
    user_id = "user-one"
    note_path = "Misplaced Topic.md"
    root = tmp_path / user_id
    root.mkdir()
    (root / note_path).write_text(
        "Topic content at the wrong level.\n", encoding="utf-8"
    )
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(syncthing_audit, "_BACKEND_VAULTS_DIR", tmp_path)
    monkeypatch.setattr(syncthing_audit, "record_vault_change", record)

    await syncthing_audit._record_event(
        {
            "folder": f"vault-{user_id}",
            "type": "file",
            "item": note_path,
            "action": "update",
            "error": "",
        }
    )

    assert recorded == [
        {
            "user_id": user_id,
            "conversation_id": None,
            "operation": "update",
            "note_path": note_path,
            "after": "Topic content at the wrong level.\n",
            "agent_mode": False,
            "summary": "inbound Syncthing update",
        }
    ]


@pytest.mark.parametrize("note_path", ["People.md", "Conversations.md", "Topics.md"])
def test_canonical_root_hubs_remain_scaffold(note_path):
    assert syncthing_audit._is_scaffold(note_path)
