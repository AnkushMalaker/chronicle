"""Tests for portable Chronicle data archives."""

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from bson import ObjectId

from advanced_omi_backend.services import data_archive
from advanced_omi_backend.services.data_archive import (
    ArchiveError,
    clear_vault_contents,
    create_data_archive,
    import_data_archive,
    verify_data_archive,
)


class AsyncCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def __aiter__(self):
        self.iterator = iter(self.documents)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    def sort(self, _fields):
        return self


class FakeCollection:
    def __init__(self, documents=()):
        self.documents = {document["_id"]: document for document in documents}
        self.delete_calls = 0

    def find(self, _query, projection=None):
        return AsyncCursor(self.documents.values())

    async def delete_many(self, _query):
        deleted = len(self.documents)
        self.documents.clear()
        self.delete_calls += 1
        return SimpleNamespace(deleted_count=deleted)

    async def bulk_write(self, operations, ordered):
        assert ordered is False
        for operation in operations:
            self.documents[operation._filter["_id"]] = operation._doc


class FakeDatabase:
    name = "chronicle_test"

    def __init__(self, collections):
        self.collections = {
            name: FakeCollection(documents) for name, documents in collections.items()
        }

    async def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        self.collections.setdefault(name, FakeCollection())
        return self.collections[name]


@pytest.mark.asyncio
async def test_archive_round_trips_bson_audio_and_files(tmp_path: Path):
    created_at = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    conversation_id = "conversation-1"
    source = FakeDatabase(
        {
            "conversations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": conversation_id,
                    "created_at": created_at,
                    "transcript_versions": [
                        {"version_id": "v1", "transcript": "Exact transcript"}
                    ],
                }
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "conversation_id": conversation_id,
                    "chunk_index": 0,
                    "audio_data": b"\x00opus\xffbytes",
                    "created_at": created_at,
                }
            ],
            "memory_audit": [{"_id": ObjectId(), "user_id": "user-1"}],
        }
    )
    source_data = tmp_path / "source-data"
    note = source_data / "conversation_docs" / "user-1" / "People" / "Ada.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Ada\n", encoding="utf-8")
    legacy_audio = source_data / "audio_chunks" / "legacy.wav"
    legacy_audio.parent.mkdir(parents=True)
    legacy_audio.write_bytes(b"RIFF-audio")

    archive_path = tmp_path / "backup.chronicle"
    summary = await create_data_archive(source, archive_path, data_dir=source_data)

    assert summary.documents == 3
    assert summary.files == 2
    manifest = verify_data_archive(archive_path)
    assert manifest["collections"]["audio_chunks"]["documents"] == 1

    target = FakeDatabase({"conversations": [{"_id": ObjectId(), "old": True}]})
    target_data = tmp_path / "target-data"
    stale_note = target_data / "conversation_docs" / "user-1" / "stale.md"
    stale_note.parent.mkdir(parents=True)
    stale_note.write_text("stale", encoding="utf-8")
    imported = await import_data_archive(
        target,
        archive_path,
        data_dir=target_data,
        replace=True,
    )

    assert imported.documents == 3
    assert imported.files == 2
    assert len(target["conversations"].documents) == 1
    restored_audio = next(iter(target["audio_chunks"].documents.values()))
    assert restored_audio["audio_data"] == b"\x00opus\xffbytes"
    # BSON stores UTC milliseconds; the default Mongo codec returns a naive UTC value.
    assert restored_audio["created_at"] == created_at.replace(tzinfo=None)
    assert not stale_note.exists()
    assert (
        target_data / "conversation_docs/user-1/People/Ada.md"
    ).read_text() == "# Ada\n"
    assert (target_data / "audio_chunks/legacy.wav").read_bytes() == b"RIFF-audio"


@pytest.mark.asyncio
async def test_fresh_memory_skips_derived_collection_and_files(tmp_path: Path):
    source = FakeDatabase(
        {
            "conversations": [{"_id": ObjectId(), "conversation_id": "conv"}],
            "memory_audit": [{"_id": ObjectId(), "user_id": "user"}],
        }
    )
    source_data = tmp_path / "source"
    note = source_data / "conversation_docs/user/People/Old.md"
    note.parent.mkdir(parents=True)
    note.write_text("old", encoding="utf-8")
    archive_path = tmp_path / "fresh.chronicle"
    await create_data_archive(source, archive_path, data_dir=source_data)

    existing_audit = {"_id": ObjectId(), "user_id": "existing"}
    target = FakeDatabase({"memory_audit": [existing_audit]})
    target_data = tmp_path / "target"
    result = await import_data_archive(
        target,
        archive_path,
        data_dir=target_data,
        fresh_memory=True,
        restore_files=False,
    )

    assert result.skipped_collections == ("memory_audit",)
    assert list(target["memory_audit"].documents.values()) == [existing_audit]
    assert not target_data.exists()


def test_verify_rejects_duplicate_or_tampered_members(tmp_path: Path):
    archive_path = tmp_path / "invalid.chronicle"
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("manifest.json", "{}")

    with pytest.raises(ArchiveError, match="duplicate member"):
        verify_data_archive(archive_path)


def test_clear_vault_preserves_syncthing_markers(tmp_path: Path):
    user_root = tmp_path / "user"
    (user_root / ".stfolder").mkdir(parents=True)
    (user_root / ".stignore").write_text("ignore", encoding="utf-8")
    (user_root / "People").mkdir()
    (user_root / "People" / "Ada.md").write_text("memory", encoding="utf-8")

    deleted = clear_vault_contents(user_root)

    assert deleted == 1
    assert (user_root / ".stfolder").is_dir()
    assert (user_root / ".stignore").is_file()
    assert not (user_root / "People").exists()


@pytest.mark.asyncio
async def test_import_keeps_earliest_conversation_for_duplicate_audio(
    tmp_path: Path, caplog, monkeypatch
):
    async def decode_as_pcm(opus_data, _sample_rate, _channels):
        return b"decoded-identical-pcm"

    monkeypatch.setattr(data_archive, "decode_opus_to_pcm", decode_as_pcm)
    first_id = "first-conversation"
    duplicate_id = "duplicate-conversation"
    source = FakeDatabase(
        {
            "conversations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": duplicate_id,
                    "created_at": datetime(2026, 7, 16, tzinfo=timezone.utc),
                    "transcript_versions": [{"transcript": "second version"}],
                },
                {
                    "_id": ObjectId(),
                    "conversation_id": first_id,
                    "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                    "transcript_versions": [{"transcript": "first version"}],
                },
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "conversation_id": duplicate_id,
                    "chunk_index": 0,
                    "audio_data": b"opus-encoding-b",
                },
                {
                    "_id": ObjectId(),
                    "conversation_id": first_id,
                    "chunk_index": 0,
                    "audio_data": b"opus-encoding-a",
                },
            ],
            "annotations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": duplicate_id,
                    "value": "skip me",
                },
                {
                    "_id": ObjectId(),
                    "conversation_id": first_id,
                    "value": "keep me",
                },
            ],
        }
    )
    archive_path = tmp_path / "duplicates.chronicle"
    await create_data_archive(source, archive_path, data_dir=tmp_path / "source")

    target = FakeDatabase({})
    result = await import_data_archive(
        target,
        archive_path,
        data_dir=tmp_path / "target",
        replace=True,
    )

    conversations = list(target["conversations"].documents.values())
    assert [conversation["conversation_id"] for conversation in conversations] == [
        first_id
    ]
    assert conversations[0]["transcript_versions"] == [{"transcript": "first version"}]
    assert len(target["audio_chunks"].documents) == 1
    assert [item["value"] for item in target["annotations"].documents.values()] == [
        "keep me"
    ]
    assert result.documents == 3
    assert len(result.duplicate_audio_warnings) == 1
    warning = result.duplicate_audio_warnings[0]
    assert warning.kept_conversation_id == first_id
    assert warning.skipped_conversation_id == duplicate_id
    assert warning.kept_source == "archive"
    assert "Duplicate audio skipped during import" in caplog.text


@pytest.mark.asyncio
async def test_merge_import_does_not_duplicate_audio_already_in_database(
    tmp_path: Path, monkeypatch
):
    async def decode_as_pcm(opus_data, _sample_rate, _channels):
        return b"decoded-identical-pcm"

    monkeypatch.setattr(data_archive, "decode_opus_to_pcm", decode_as_pcm)
    existing_id = "existing-conversation"
    imported_id = "imported-conversation"
    source = FakeDatabase(
        {
            "conversations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": imported_id,
                    "created_at": datetime(2026, 7, 16, tzinfo=timezone.utc),
                }
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "conversation_id": imported_id,
                    "chunk_index": 0,
                    "audio_data": b"new-opus-encoding",
                }
            ],
        }
    )
    archive_path = tmp_path / "merge.chronicle"
    await create_data_archive(source, archive_path, data_dir=tmp_path / "source")
    target = FakeDatabase(
        {
            "conversations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": existing_id,
                    "created_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
                }
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "conversation_id": existing_id,
                    "chunk_index": 0,
                    "audio_data": b"existing-opus-encoding",
                }
            ],
        }
    )

    result = await import_data_archive(
        target,
        archive_path,
        data_dir=tmp_path / "target",
    )

    assert len(target["conversations"].documents) == 1
    assert len(target["audio_chunks"].documents) == 1
    warning = result.duplicate_audio_warnings[0]
    assert warning.kept_conversation_id == existing_id
    assert warning.skipped_conversation_id == imported_id
    assert warning.kept_source == "existing_database"


@pytest.mark.asyncio
async def test_import_keeps_first_duplicate_chunk_and_warns(tmp_path: Path):
    conversation_id = "conversation-1"
    first_id = ObjectId()
    duplicate_id = ObjectId()
    source = FakeDatabase(
        {
            "conversations": [{"_id": ObjectId(), "conversation_id": conversation_id}],
            "audio_chunks": [
                {
                    "_id": first_id,
                    "conversation_id": conversation_id,
                    "chunk_index": 0,
                    "audio_data": b"first",
                    "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                },
                {
                    "_id": duplicate_id,
                    "conversation_id": conversation_id,
                    "chunk_index": 0,
                    "audio_data": b"second",
                    "created_at": datetime(2026, 7, 16, tzinfo=timezone.utc),
                },
            ],
        }
    )
    archive_path = tmp_path / "duplicate-chunk.chronicle"
    await create_data_archive(source, archive_path, data_dir=tmp_path / "source")

    target = FakeDatabase({})
    result = await import_data_archive(
        target, archive_path, data_dir=tmp_path / "target", replace=True
    )

    chunks = list(target["audio_chunks"].documents.values())
    assert len(chunks) == 1
    assert chunks[0]["_id"] == first_id
    assert len(result.duplicate_chunk_warnings) == 1
    warning = result.duplicate_chunk_warnings[0]
    assert warning.kept_chunk_id == str(first_id)
    assert warning.skipped_chunk_id == str(duplicate_id)


@pytest.mark.asyncio
async def test_export_omits_audio_for_excluded_conversations(tmp_path: Path):
    created_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    source = FakeDatabase(
        {
            "conversations": [
                {
                    "_id": ObjectId(),
                    "conversation_id": "keep",
                    "created_at": created_at,
                },
                {"_id": ObjectId(), "conversation_id": "dup", "created_at": created_at},
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "conversation_id": "keep",
                    "chunk_index": 0,
                    "audio_data": b"keep-audio",
                    "created_at": created_at,
                },
                {
                    "_id": ObjectId(),
                    "conversation_id": "dup",
                    "chunk_index": 0,
                    "audio_data": b"already-backed-up",
                    "created_at": created_at,
                },
            ],
            "annotations": [{"_id": ObjectId(), "conversation_id": "dup"}],
        }
    )

    archive_path = tmp_path / "deduped.chronicle"
    summary = await create_data_archive(
        source,
        archive_path,
        data_dir=tmp_path / "source",
        exclude_audio_conversation_ids=["dup"],
    )

    assert summary.excluded_audio_chunks == 1
    assert summary.excluded_audio_conversations == 1
    manifest = verify_data_archive(archive_path)
    assert manifest["collections"]["audio_chunks"]["documents"] == 1
    assert manifest["excluded_audio_conversation_ids"] == ["dup"]

    target = FakeDatabase({})
    await import_data_archive(
        target, archive_path, data_dir=tmp_path / "target", replace=True
    )

    chunks = list(target["audio_chunks"].documents.values())
    assert [chunk["conversation_id"] for chunk in chunks] == ["keep"]
    # The excluded conversation itself, and its annotations, are still archived.
    conversations = [
        document["conversation_id"]
        for document in target["conversations"].documents.values()
    ]
    assert sorted(conversations) == ["dup", "keep"]
    assert len(target["annotations"].documents) == 1
