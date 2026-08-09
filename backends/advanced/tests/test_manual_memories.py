from datetime import datetime, timezone

from advanced_omi_backend.models.manual_memory import (
    ManualMemory,
    ManualMemoryAttachment,
)
from advanced_omi_backend.services.manual_memories.image import _body, write_memory_note
from advanced_omi_backend.services.memory.vault_media import promote_image_bytes
from advanced_omi_backend.services.timeline.evidence import _manual_memory_item

JPEG = b"\xff\xd8\xff" + b"manual-memory-image"


def memory(attachment: ManualMemoryAttachment, *, note: str | None = "Remember this"):
    return ManualMemory.model_construct(
        id=None,
        revision_id=None,
        memory_id=__import__("uuid").uuid4().hex,
        user_id="user-1",
        request_id="request-123",
        note=note,
        source={"kind": "share_sheet", "application": "Photos"},
        shared_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        attachments=[attachment],
        vault_path="",
        memory_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def attachment(path: str, digest: str) -> ManualMemoryAttachment:
    return ManualMemoryAttachment(
        content_type="image/jpeg",
        original_filename="kept-image.jpg",
        content_hash=digest,
        storage_path=path,
        byte_size=len(JPEG),
    )


def test_same_content_reuses_storage_but_keeps_attachment_identity(tmp_path):
    first_path, digest = promote_image_bytes(JPEG, "image/jpeg", tmp_path)
    second_path, second_digest = promote_image_bytes(JPEG, "image/jpeg", tmp_path)

    first = attachment(first_path, digest)
    second = attachment(second_path, second_digest)

    assert first.content_hash == second.content_hash
    assert first.storage_path == second.storage_path
    assert first.attachment_id != second.attachment_id


def test_initial_vault_note_is_searchable_before_enrichment(tmp_path, monkeypatch):
    path, digest = promote_image_bytes(JPEG, "image/jpeg", tmp_path)
    item = memory(attachment(path, digest))
    monkeypatch.setattr(
        "advanced_omi_backend.services.manual_memories.image.vault_note_lock",
        lambda _user_id: __import__("contextlib").nullcontext(),
    )

    note_path = write_memory_note(item, tmp_path)
    contents = (tmp_path / note_path).read_text()

    assert note_path == f"Manual Memories/{item.memory_id}.md"
    assert "> Remember this" in contents
    assert f"![[../{path}]]" in contents


def test_user_note_precedes_generated_enrichment():
    item = memory(attachment("_media/image.jpg", "a" * 64))
    item.attachments[0].description = "Generated description"
    item.attachments[0].extracted_text = "VERBATIM TEXT"

    body = _body(item)

    assert body.index("Remember this") < body.index("Generated description")
    assert "## Extracted text\n\nVERBATIM TEXT" in body


def test_manual_memory_is_deliberate_timeline_evidence():
    item = memory(attachment("_media/image.jpg", "a" * 64))

    evidence = _manual_memory_item(item)

    assert evidence.role == "user_action"
    assert evidence.excerpt == "Remember this"
    assert evidence.started_at == item.shared_at
    assert evidence.metadata["source_kind"] == "manual_memory"
