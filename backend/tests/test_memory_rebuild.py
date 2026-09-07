"""Tests for clean, ordered Markdown-vault reconstruction."""

import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.memory import rebuild
from backend.services.memory.rebuild import (
    RebuildConversation,
    RebuildPlan,
    RebuildStage,
    build_rebuild_plan,
    create_vault_backup,
    execute_memory_rebuild,
)


class AuditCollection:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count
        self.query = None
        self.inserted = []

    async def delete_many(self, query):
        self.query = query
        return SimpleNamespace(deleted_count=self.deleted_count)

    async def insert_many(self, documents):
        self.inserted.extend(documents)


class FakeDatabase:
    def __init__(self, audit):
        self.audit = audit

    def __getitem__(self, name):
        assert name == "memory_audit"
        return self.audit


class PlanCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, _fields):
        return self

    def __aiter__(self):
        self.iterator = iter(self.documents)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class ConversationCollection:
    def __init__(self, documents):
        self.documents = documents
        self.query = None

    def find(self, query, projection):
        self.query = query
        return PlanCursor(self.documents)


class AudioCollection:
    def __init__(self, conversation_ids):
        self.conversation_ids = conversation_ids
        self.query = None

    async def distinct(self, field, query):
        assert field == "conversation_id"
        self.query = query
        return self.conversation_ids


def test_active_rebuild_jobs_catches_user_scoped_timeline_job(monkeypatch):
    empty_registry = SimpleNamespace(get_job_ids=lambda: [])

    def queue(job_ids):
        return SimpleNamespace(
            get_job_ids=lambda: list(job_ids),
            started_job_registry=empty_registry,
            deferred_job_registry=empty_registry,
            scheduled_job_registry=empty_registry,
            connection=object(),
        )

    timeline_job = SimpleNamespace(
        meta={},
        func_name=("backend.workers.timeline_jobs." "rebuild_timeline_day_job"),
        args=("user-a", "2026-01-06", "Asia/Kolkata"),
    )
    monkeypatch.setattr(rebuild, "transcription_queue", queue([]))
    monkeypatch.setattr(rebuild, "memory_queue", queue(["day-retry"]))
    monkeypatch.setattr(rebuild, "default_queue", queue([]))
    monkeypatch.setattr(
        rebuild.Job,
        "fetch",
        staticmethod(lambda job_id, connection: timeline_job),
    )

    assert rebuild._active_rebuild_jobs(set(), {"user-a"}) == ["day-retry"]
    assert rebuild._active_rebuild_jobs(set(), {"user-b"}) == []


def test_vault_backup_manifest_records_description_and_file_hashes(tmp_path: Path):
    vault = tmp_path / "conversation_docs" / "user-1"
    vault.mkdir(parents=True)
    note = vault / "Daily" / "2026-08-11.md"
    note.parent.mkdir()
    note.write_text("# The rebuilt day\n", encoding="utf-8")

    backup = create_vault_backup(
        tmp_path,
        ("user-1",),
        tmp_path / "backups",
        description="reingest after speaker fix",
    )

    assert backup is not None
    with tarfile.open(backup, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        archived_note = archive.extractfile(
            "conversation_docs/user-1/Daily/2026-08-11.md"
        ).read()
    assert manifest["format"] == "chronicle-memory-vault-backup"
    assert manifest["schema_version"] == 1
    assert manifest["description"] == "reingest after speaker fix"
    assert manifest["user_ids"] == ["user-1"]
    assert manifest["vault_roots"] == ["conversation_docs"]
    assert manifest["files"]["conversation_docs/user-1/Daily/2026-08-11.md"] == {
        "bytes": len(archived_note),
        "sha256": hashlib.sha256(archived_note).hexdigest(),
    }


@pytest.mark.asyncio
async def test_build_rebuild_plan_collects_async_cursor():
    collection = ConversationCollection(
        [
            {
                "conversation_id": "conversation-1",
                "user_id": "user-1",
                "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                "active_transcript_version": "version-1",
                "transcript_versions": [],
            }
        ]
    )
    database = {"conversations": collection}

    plan = await build_rebuild_plan(database)

    assert plan.count == 1
    assert plan.user_ids == ("user-1",)
    assert plan.conversations[0].conversation_id == "conversation-1"
    assert plan.conversations[0].transcript_version_id == "version-1"


@pytest.mark.asyncio
async def test_speaker_plan_includes_memory_excluded_conversations():
    collection = ConversationCollection(
        [
            {
                "conversation_id": "excluded-conversation",
                "user_id": "user-1",
                "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                "active_transcript_version": "version-1",
                "transcript_versions": [],
                "audio_ranges": [],
                "memory_excluded": True,
            }
        ]
    )

    plan = await build_rebuild_plan(
        {"conversations": collection},
        from_stage=RebuildStage.SPEAKERS,
    )

    assert plan.count == 1
    assert plan.speaker_count == 0
    assert plan.memory_count == 0
    assert plan.conversations[0].memory_excluded is True
    assert plan.conversations[0].has_audio is False
    assert "memory_excluded" not in collection.query


@pytest.mark.asyncio
async def test_speaker_plan_unwraps_previous_speaker_version():
    collection = ConversationCollection(
        [
            {
                "conversation_id": "conversation-1",
                "user_id": "user-1",
                "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                "active_transcript_version": "speaker-version",
                "transcript_versions": [
                    {"version_id": "source-version", "metadata": {}},
                    {
                        "version_id": "speaker-version",
                        "metadata": {
                            "reprocessing_type": "speaker_diarization",
                            "source_version_id": "source-version",
                        },
                    },
                ],
            }
        ]
    )

    plan = await build_rebuild_plan(
        {
            "conversations": collection,
            "audio_chunks": AudioCollection(["conversation-1"]),
        },
        from_stage=RebuildStage.SPEAKERS,
    )

    item = plan.conversations[0]
    assert item.transcript_version_id == "source-version"
    assert item.active_transcript_version_id == "speaker-version"


@pytest.mark.asyncio
async def test_execute_rebuild_chains_each_user_chronologically(
    monkeypatch, tmp_path: Path
):
    created = datetime(2026, 7, 15, tzinfo=timezone.utc)
    plan = RebuildPlan(
        conversations=(
            RebuildConversation("a-first", "user-a", created, "version-a1"),
            RebuildConversation("a-second", "user-a", created, "version-a2"),
            RebuildConversation("b-first", "user-b", created, "version-b1"),
        ),
        user_ids=("user-a", "user-b"),
    )
    for user_id in plan.user_ids:
        root = tmp_path / "conversation_docs" / user_id
        root.mkdir(parents=True)
        (root / "old.md").write_text("old", encoding="utf-8")
        (root / ".stignore").write_text("sync", encoding="utf-8")

    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids, _users: [])
    enqueued = []

    def fake_enqueue(conversation_id, **kwargs):
        job = SimpleNamespace(id=kwargs["job_id"])
        enqueued.append((conversation_id, kwargs, job))
        return job

    monkeypatch.setattr(rebuild, "enqueue_memory_processing", fake_enqueue)
    audit = AuditCollection(deleted_count=7)

    result = await execute_memory_rebuild(
        FakeDatabase(audit),
        plan,
        data_dir=tmp_path,
        backup_dir=None,
    )

    assert [item[0] for item in enqueued] == ["a-first", "a-second", "b-first"]
    assert enqueued[0][1]["depends_on"] is None
    assert enqueued[1][1]["depends_on"] is enqueued[0][2]
    assert enqueued[2][1]["depends_on"] is None
    assert all(item[1]["cause"].value == "memory_rebuild" for item in enqueued)
    assert all(
        item[1]["job_timeout"] == rebuild.MEMORY_REBUILD_JOB_TIMEOUT
        for item in enqueued
    )
    assert result.deleted_vault_files == 2
    assert result.deleted_audit_entries == 0
    assert audit.query is None
    assert {entry["user_id"] for entry in audit.inserted} == {"user-a", "user-b"}
    assert all(
        entry["extra"]["rebuild_epoch"] == result.run_id for entry in audit.inserted
    )
    for user_id in plan.user_ids:
        assert (tmp_path / "conversation_docs" / user_id / ".stignore").exists()
        assert not (tmp_path / "conversation_docs" / user_id / "old.md").exists()


@pytest.mark.asyncio
async def test_execute_rebuild_from_speakers_continues_after_failed_speaker(
    monkeypatch, tmp_path: Path
):
    created = datetime(2026, 7, 15, tzinfo=timezone.utc)
    plan = RebuildPlan(
        conversations=(
            RebuildConversation("first", "user-a", created, "version-1"),
            RebuildConversation("second", "user-a", created, "version-2"),
        ),
        user_ids=("user-a",),
    )
    speaker_calls = []
    memory_calls = []

    def fake_speaker(item, *, run_id, sequence, depends_on):
        job = SimpleNamespace(id=f"speaker-{sequence}")
        speaker_calls.append((item, run_id, sequence, depends_on, job))
        return job

    def fake_memory(conversation_id, **kwargs):
        job = SimpleNamespace(id=f"memory-{conversation_id}")
        memory_calls.append((conversation_id, kwargs, job))
        return job

    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids, _users: [])
    monkeypatch.setattr(rebuild, "_enqueue_speaker_rebuild", fake_speaker)
    monkeypatch.setattr(rebuild, "enqueue_memory_processing", fake_memory)

    result = await execute_memory_rebuild(
        FakeDatabase(AuditCollection(deleted_count=0)),
        plan,
        data_dir=tmp_path,
        backup_dir=None,
        from_stage=RebuildStage.SPEAKERS,
    )

    assert [call[0].conversation_id for call in speaker_calls] == ["first", "second"]
    assert speaker_calls[0][3] is None
    assert speaker_calls[1][3] == "speaker-1"
    assert [call[0] for call in memory_calls] == ["first", "second"]
    first_dependency = memory_calls[0][1]["depends_on"]
    assert first_dependency.dependencies == ["speaker-2"]
    assert first_dependency.allow_failure is True
    assert memory_calls[1][1]["depends_on"] is memory_calls[0][2]
    assert result.from_stage is RebuildStage.SPEAKERS
    assert result.speaker_jobs == ("speaker-1", "speaker-2")


def test_speaker_rebuild_dependency_allows_previous_failure(monkeypatch):
    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=kwargs["job_id"])

    def fake_enqueue_kwargs(_stage, _meta, depends_on=None):
        return {"depends_on": depends_on}

    monkeypatch.setattr(
        rebuild, "transcription_queue", SimpleNamespace(enqueue=fake_enqueue)
    )
    monkeypatch.setattr(rebuild, "post_conv_enqueue_kwargs", fake_enqueue_kwargs)
    item = RebuildConversation(
        "conversation-1",
        "user-1",
        datetime(2026, 7, 15, tzinfo=timezone.utc),
        "version-1",
    )

    rebuild._enqueue_speaker_rebuild(
        item,
        run_id="run-id",
        sequence=2,
        depends_on="previous-speaker-job",
    )

    dependency = captured["depends_on"]
    assert dependency.dependencies == ["previous-speaker-job"]
    assert dependency.allow_failure is True


@pytest.mark.asyncio
async def test_speaker_rebuild_skips_conversation_without_audio_but_rebuilds_memory(
    monkeypatch, tmp_path: Path
):
    plan = RebuildPlan(
        conversations=(
            RebuildConversation(
                "transcript-only",
                "user-a",
                datetime(2026, 7, 15, tzinfo=timezone.utc),
                "version-1",
                has_audio=False,
            ),
        ),
        user_ids=("user-a",),
    )
    memory_calls = []

    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids, _users: [])
    monkeypatch.setattr(
        rebuild,
        "_enqueue_speaker_rebuild",
        lambda *args, **kwargs: pytest.fail("speaker job should not be enqueued"),
    )

    def fake_memory(conversation_id, **kwargs):
        memory_calls.append(conversation_id)
        return SimpleNamespace(id="memory-job")

    monkeypatch.setattr(rebuild, "enqueue_memory_processing", fake_memory)

    result = await execute_memory_rebuild(
        FakeDatabase(AuditCollection(deleted_count=0)),
        plan,
        data_dir=tmp_path,
        from_stage=RebuildStage.SPEAKERS,
    )

    assert memory_calls == ["transcript-only"]
    assert result.speaker_jobs == ()
    assert result.skipped_speaker_conversations == ("transcript-only",)
