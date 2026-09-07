"""Tests for portable Chronicle data archives."""

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from bson import ObjectId

from backend.services.data_archive import (
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

    async def count_documents(self, _query):
        return len(self.documents)

    async def delete_many(self, query):
        ids = query.get("_id", {}).get("$in") if query else None
        if ids is None:
            deleted = len(self.documents)
            self.documents.clear()
        else:
            deleted = 0
            for document_id in ids:
                if self.documents.pop(document_id, None) is not None:
                    deleted += 1
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
    capture_session_id = "capture-1"
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
                    "capture_session_id": capture_session_id,
                    "sequence": 0,
                    "audio_data": b"\x00opus\xffbytes",
                    "captured_at": created_at,
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
async def test_export_reports_completed_database_file_and_finalize_stages(
    tmp_path: Path,
):
    source = FakeDatabase(
        {
            "conversations": [
                {"_id": ObjectId(), "conversation_id": "conversation-progress"}
            ]
        }
    )
    source_data = tmp_path / "source"
    note = source_data / "conversation_docs/user/Daily/2026-08-10.md"
    note.parent.mkdir(parents=True)
    note.write_text("progress", encoding="utf-8")
    events = []

    await create_data_archive(
        source,
        tmp_path / "progress.chronicle",
        data_dir=source_data,
        progress=events.append,
    )

    completed = [event for event in events if event.completed]
    assert [event.stage for event in completed] == [
        "export_database",
        "export_files",
        "finalize_archive",
    ]
    assert completed[0].current == completed[0].total == 1
    assert completed[1].current == completed[1].total == len("progress")


@pytest.mark.asyncio
async def test_import_reports_named_stage_and_item_progress(tmp_path: Path):
    conversation_id = "conversation-progress"
    source = FakeDatabase(
        {
            "conversations": [{"_id": ObjectId(), "conversation_id": conversation_id}],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "capture_session_id": "capture-progress",
                    "sequence": 0,
                    "audio_data": b"progress-audio",
                    "captured_at": datetime(2026, 8, 10, tzinfo=timezone.utc),
                }
            ],
        }
    )
    source_data = tmp_path / "source"
    note = source_data / "conversation_docs/user/People/Progress.md"
    note.parent.mkdir(parents=True)
    note.write_text("progress", encoding="utf-8")
    archive_path = tmp_path / "progress.chronicle"
    await create_data_archive(source, archive_path, data_dir=source_data)

    events = []
    await import_data_archive(
        FakeDatabase({}),
        archive_path,
        data_dir=tmp_path / "target",
        replace=True,
        progress=events.append,
    )

    completed_stages = {event.stage for event in events if event.completed}
    assert completed_stages == {
        "verify",
        "restore_database",
        "restore_files",
    }
    for stage in completed_stages:
        stage_events = [event for event in events if event.stage == stage]
        assert stage_events[-1].completed is True
        assert stage_events[-1].current == stage_events[-1].total
    restore_events = [event for event in events if event.stage == "restore_database"]
    assert any("audio_chunks" in event.detail for event in restore_events)


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
async def test_import_preserves_distinct_captures_with_identical_audio(tmp_path: Path):
    captured_at = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    source = FakeDatabase(
        {
            "conversations": [
                {"_id": ObjectId(), "conversation_id": "first"},
                {"_id": ObjectId(), "conversation_id": "second"},
            ],
            "audio_chunks": [
                {
                    "_id": ObjectId(),
                    "capture_session_id": "capture-first",
                    "sequence": 0,
                    "audio_data": b"same-audio",
                    "captured_at": captured_at,
                },
                {
                    "_id": ObjectId(),
                    "capture_session_id": "capture-second",
                    "sequence": 0,
                    "audio_data": b"same-audio",
                    "captured_at": captured_at,
                },
            ],
        }
    )
    archive_path = tmp_path / "distinct-captures.chronicle"
    await create_data_archive(source, archive_path, data_dir=tmp_path / "source")

    target = FakeDatabase({})
    result = await import_data_archive(
        target,
        archive_path,
        data_dir=tmp_path / "target",
        replace=True,
    )

    assert result.documents == 4
    assert len(target["conversations"].documents) == 2
    assert len(target["audio_chunks"].documents) == 2


@pytest.mark.asyncio
async def test_pre_cutover_audio_snapshot_restores_exactly_in_replace_mode(
    tmp_path: Path,
):
    chunk = {
        "_id": ObjectId(),
        "conversation_id": "pre-cutover-owner",
        "chunk_index": 7,
        "start_time": 70.0,
        "end_time": 80.0,
        "duration": 10.0,
        "audio_data": b"exact-pre-cutover-opus",
    }
    archive_path = tmp_path / "pre-cutover.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [chunk]}),
        archive_path,
        data_dir=tmp_path / "source",
    )
    target = FakeDatabase({})

    await import_data_archive(
        target,
        archive_path,
        data_dir=tmp_path / "target",
        replace=True,
    )

    assert target["audio_chunks"].documents[chunk["_id"]] == chunk


@pytest.mark.asyncio
async def test_pre_cutover_audio_snapshot_refuses_ambiguous_merge_mode(tmp_path: Path):
    chunk = {
        "_id": ObjectId(),
        "conversation_id": "pre-cutover-owner",
        "chunk_index": 0,
        "audio_data": b"pre-cutover-opus",
    }
    archive_path = tmp_path / "pre-cutover.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [chunk]}),
        archive_path,
        data_dir=tmp_path / "source",
    )

    with pytest.raises(ArchiveError, match="require --replace"):
        await import_data_archive(
            FakeDatabase({}),
            archive_path,
            data_dir=tmp_path / "target",
        )


@pytest.mark.asyncio
async def test_merge_import_is_idempotent_by_immutable_chunk_id(tmp_path: Path):
    chunk_id = ObjectId()
    captured_at = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    chunk = {
        "_id": chunk_id,
        "capture_session_id": "capture-one",
        "sequence": 0,
        "audio_data": b"same-chunk",
        "captured_at": captured_at,
    }
    archive_path = tmp_path / "idempotent.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [chunk]}),
        archive_path,
        data_dir=tmp_path / "source",
    )
    target = FakeDatabase({"audio_chunks": [dict(chunk)]})

    await import_data_archive(target, archive_path, data_dir=tmp_path / "target")

    assert list(target["audio_chunks"].documents) == [chunk_id]


@pytest.mark.asyncio
async def test_merge_rejects_conflicting_capture_sequence(tmp_path: Path):
    captured_at = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    archived = {
        "_id": ObjectId(),
        "capture_session_id": "capture-one",
        "sequence": 7,
        "audio_data": b"archive",
        "captured_at": captured_at,
    }
    existing = {
        "_id": ObjectId(),
        "capture_session_id": "capture-one",
        "sequence": 7,
        "audio_data": b"database",
        "captured_at": captured_at,
    }
    archive_path = tmp_path / "conflict.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [archived]}),
        archive_path,
        data_dir=tmp_path / "source",
    )

    with pytest.raises(
        ArchiveError, match="Conflicting immutable audio chunk identity"
    ):
        await import_data_archive(
            FakeDatabase({"audio_chunks": [existing]}),
            archive_path,
            data_dir=tmp_path / "target",
        )


@pytest.mark.asyncio
async def test_incremental_archive_omits_exact_base_chunk_ids(tmp_path: Path):
    captured_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    base_chunks = [
        {
            "_id": ObjectId(),
            "capture_session_id": "capture-base",
            "sequence": sequence,
            "audio_data": f"base-{sequence}".encode(),
            "captured_at": captured_at,
        }
        for sequence in range(2)
    ]
    base_path = tmp_path / "base.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": base_chunks}),
        base_path,
        data_dir=tmp_path / "base-data",
    )

    new_chunk = {
        "_id": ObjectId(),
        "capture_session_id": "capture-new",
        "sequence": 0,
        "audio_data": b"new",
        "captured_at": captured_at,
    }
    incremental_path = tmp_path / "incremental.chronicle"
    summary = await create_data_archive(
        FakeDatabase(
            {
                "audio_chunks": [*base_chunks, new_chunk],
                "conversations": [
                    {"_id": ObjectId(), "conversation_id": "semantic-claim"}
                ],
            }
        ),
        incremental_path,
        data_dir=tmp_path / "incremental-data",
        base_archives=[base_path],
    )

    assert summary.excluded_audio_chunks == 2
    manifest = verify_data_archive(incremental_path)
    assert manifest["collections"]["audio_chunks"]["documents"] == 1
    assert set(manifest["excluded_audio_chunk_ids"]) == {
        str(chunk["_id"]) for chunk in base_chunks
    }
    assert manifest["base_archives"][0]["filename"] == base_path.name

    target = FakeDatabase({})
    await import_data_archive(
        target, base_path, data_dir=tmp_path / "restore", replace=True
    )
    await import_data_archive(target, incremental_path, data_dir=tmp_path / "restore")
    assert set(target["audio_chunks"].documents) == {
        *(chunk["_id"] for chunk in base_chunks),
        new_chunk["_id"],
    }

    with pytest.raises(ArchiveError, match="Incremental archive"):
        await import_data_archive(
            FakeDatabase({}),
            incremental_path,
            data_dir=tmp_path / "invalid-restore",
            replace=True,
        )


@pytest.mark.asyncio
async def test_incremental_archive_reexports_audio_when_same_id_metadata_changes(
    tmp_path: Path,
):
    chunk_id = ObjectId()
    captured_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    base_chunk = {
        "_id": chunk_id,
        "capture_session_id": "capture-metadata-change",
        "sequence": 0,
        "audio_data": b"immutable-opus",
        "captured_at": captured_at,
        "deleted": False,
    }
    base_path = tmp_path / "base-audio-metadata.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [base_chunk]}),
        base_path,
        data_dir=tmp_path / "base-data",
    )

    changed_chunk = {
        **base_chunk,
        "deleted": True,
        "deletion_reason": "operator_quarantine",
    }
    incremental_path = tmp_path / "incremental-audio-metadata.chronicle"
    summary = await create_data_archive(
        FakeDatabase({"audio_chunks": [changed_chunk]}),
        incremental_path,
        data_dir=tmp_path / "incremental-data",
        base_archives=[base_path],
    )

    assert summary.excluded_audio_chunks == 0
    manifest = verify_data_archive(incremental_path)
    assert manifest["collections"]["audio_chunks"]["documents"] == 1
    assert manifest["excluded_audio_chunk_ids"] == []
    assert str(chunk_id) in manifest["document_digests"]["audio_chunks"]

    target = FakeDatabase({})
    await import_data_archive(
        target, base_path, data_dir=tmp_path / "restore", replace=True
    )
    await import_data_archive(target, incremental_path, data_dir=tmp_path / "restore")
    restored = target["audio_chunks"].documents[chunk_id]
    assert restored["audio_data"] == b"immutable-opus"
    assert restored["deleted"] is True
    assert restored["deletion_reason"] == "operator_quarantine"


@pytest.mark.asyncio
async def test_incremental_archive_tombstones_removed_audio_chunks(tmp_path: Path):
    captured_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    kept = {
        "_id": ObjectId(),
        "capture_session_id": "capture-audio-delete",
        "sequence": 0,
        "audio_data": b"kept",
        "captured_at": captured_at,
    }
    removed = {
        "_id": ObjectId(),
        "capture_session_id": "capture-audio-delete",
        "sequence": 1,
        "audio_data": b"removed",
        "captured_at": captured_at,
    }
    base_path = tmp_path / "base-audio-delete.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [kept, removed]}),
        base_path,
        data_dir=tmp_path / "base-data",
    )

    incremental_path = tmp_path / "incremental-audio-delete.chronicle"
    summary = await create_data_archive(
        FakeDatabase({"audio_chunks": [kept]}),
        incremental_path,
        data_dir=tmp_path / "incremental-data",
        base_archives=[base_path],
    )

    assert summary.excluded_audio_chunks == 1
    manifest = verify_data_archive(incremental_path)
    assert manifest["collections"]["audio_chunks"]["documents"] == 0
    assert manifest["deleted_document_ids"]["audio_chunks"] == [str(removed["_id"])]

    target = FakeDatabase({})
    await import_data_archive(
        target, base_path, data_dir=tmp_path / "restore", replace=True
    )
    await import_data_archive(target, incremental_path, data_dir=tmp_path / "restore")
    assert set(target["audio_chunks"].documents) == {kept["_id"]}


@pytest.mark.asyncio
async def test_incremental_restore_rejects_changed_base_audio_before_mutation(
    tmp_path: Path,
):
    chunk = {
        "_id": ObjectId(),
        "capture_session_id": "capture-audio-verify",
        "sequence": 0,
        "audio_data": b"original-opus",
        "captured_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
    }
    base_path = tmp_path / "base-audio-verify.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [chunk]}),
        base_path,
        data_dir=tmp_path / "base-data",
    )

    added = {"_id": ObjectId(), "conversation_id": "must-not-be-imported"}
    incremental_path = tmp_path / "incremental-audio-verify.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [chunk], "conversations": [added]}),
        incremental_path,
        data_dir=tmp_path / "incremental-data",
        base_archives=[base_path],
    )

    target = FakeDatabase({})
    await import_data_archive(
        target, base_path, data_dir=tmp_path / "restore", replace=True
    )
    target["audio_chunks"].documents[chunk["_id"]]["audio_data"] = b"changed-opus"

    with pytest.raises(ArchiveError, match="base document changed"):
        await import_data_archive(
            target, incremental_path, data_dir=tmp_path / "restore"
        )

    assert target["conversations"].documents == {}


@pytest.mark.asyncio
async def test_incremental_chain_carries_forward_inherited_chunk_ids(tmp_path: Path):
    captured_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    first = {
        "_id": ObjectId(),
        "capture_session_id": "capture-first",
        "sequence": 0,
        "audio_data": b"first",
        "captured_at": captured_at,
    }
    second = {
        "_id": ObjectId(),
        "capture_session_id": "capture-second",
        "sequence": 0,
        "audio_data": b"second",
        "captured_at": captured_at,
    }
    first_path = tmp_path / "first.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [first]}),
        first_path,
        data_dir=tmp_path / "first-data",
    )
    second_path = tmp_path / "second.chronicle"
    await create_data_archive(
        FakeDatabase({"audio_chunks": [first, second]}),
        second_path,
        data_dir=tmp_path / "second-data",
        base_archives=[first_path],
    )

    third_path = tmp_path / "third.chronicle"
    summary = await create_data_archive(
        FakeDatabase({"audio_chunks": [first, second]}),
        third_path,
        data_dir=tmp_path / "third-data",
        base_archives=[second_path],
    )

    assert summary.excluded_audio_chunks == 2
    manifest = verify_data_archive(third_path)
    assert set(manifest["excluded_audio_chunk_ids"]) == {
        str(first["_id"]),
        str(second["_id"]),
    }
    assert manifest["collections"]["audio_chunks"]["documents"] == 0


@pytest.mark.asyncio
async def test_incremental_archive_deduplicates_documents_and_files_with_tombstones(
    tmp_path: Path,
):
    conversation = {
        "_id": ObjectId(),
        "conversation_id": "conversation-one",
        "title": "unchanged",
    }
    screenshot = {
        "_id": ObjectId(),
        "kind": "screen_context",
        "source_item_id": "screen-one",
        "media_data": b"large-screenshot-bytes",
    }
    removed = {
        "_id": ObjectId(),
        "kind": "screen_context",
        "source_item_id": "removed-screen",
        "media_data": b"removed",
    }
    base_data = tmp_path / "base-data"
    unchanged_file = base_data / "conversation_docs/user/screens/unchanged.webp"
    removed_file = base_data / "conversation_docs/user/screens/removed.webp"
    unchanged_file.parent.mkdir(parents=True)
    unchanged_file.write_bytes(b"unchanged-screen")
    removed_file.write_bytes(b"removed-screen")
    base_path = tmp_path / "base-evidence.chronicle"
    await create_data_archive(
        FakeDatabase(
            {
                "conversations": [conversation],
                "device_input_items": [screenshot, removed],
            }
        ),
        base_path,
        data_dir=base_data,
    )

    added = {
        "_id": ObjectId(),
        "kind": "screen_context",
        "source_item_id": "screen-two",
        "media_data": b"new-screenshot-bytes",
    }
    incremental_data = tmp_path / "incremental-data"
    incremental_unchanged = (
        incremental_data / "conversation_docs/user/screens/unchanged.webp"
    )
    incremental_added = incremental_data / "conversation_docs/user/screens/new.webp"
    incremental_unchanged.parent.mkdir(parents=True)
    incremental_unchanged.write_bytes(b"unchanged-screen")
    incremental_added.write_bytes(b"new-screen")
    incremental_path = tmp_path / "incremental-evidence.chronicle"
    summary = await create_data_archive(
        FakeDatabase(
            {
                "conversations": [conversation],
                "device_input_items": [screenshot, added],
            }
        ),
        incremental_path,
        data_dir=incremental_data,
        base_archives=[base_path],
    )

    assert summary.excluded_documents == 2
    assert summary.excluded_files == 1
    manifest = verify_data_archive(incremental_path)
    assert manifest["collections"]["conversations"]["documents"] == 0
    assert manifest["collections"]["device_input_items"]["documents"] == 1
    assert manifest["deleted_document_ids"]["device_input_items"] == [
        str(removed["_id"])
    ]
    assert manifest["excluded_data_files"] == [
        "files/conversation_docs/user/screens/unchanged.webp"
    ]
    assert manifest["deleted_data_files"] == [
        "files/conversation_docs/user/screens/removed.webp"
    ]

    target = FakeDatabase({})
    target_data = tmp_path / "restore-evidence"
    await import_data_archive(target, base_path, data_dir=target_data, replace=True)
    await import_data_archive(target, incremental_path, data_dir=target_data)
    assert set(target["device_input_items"].documents) == {
        screenshot["_id"],
        added["_id"],
    }
    assert (
        target_data / "conversation_docs/user/screens/unchanged.webp"
    ).read_bytes() == b"unchanged-screen"
    assert (
        target_data / "conversation_docs/user/screens/new.webp"
    ).read_bytes() == b"new-screen"
    assert not (target_data / "conversation_docs/user/screens/removed.webp").exists()


@pytest.mark.asyncio
async def test_incremental_restore_fails_before_mutation_without_its_base(
    tmp_path: Path,
):
    document = {"_id": ObjectId(), "conversation_id": "base-only"}
    base_path = tmp_path / "base-required.chronicle"
    await create_data_archive(
        FakeDatabase({"conversations": [document]}),
        base_path,
        data_dir=tmp_path / "base-required-data",
    )
    incremental_path = tmp_path / "incremental-required.chronicle"
    await create_data_archive(
        FakeDatabase({"conversations": [document]}),
        incremental_path,
        data_dir=tmp_path / "incremental-required-data",
        base_archives=[base_path],
    )
    target = FakeDatabase({})

    with pytest.raises(ArchiveError, match="base is incomplete"):
        await import_data_archive(
            target,
            incremental_path,
            data_dir=tmp_path / "missing-base-restore",
        )

    assert target["conversations"].documents == {}
