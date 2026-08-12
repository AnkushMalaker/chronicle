"""Reading and converting the pre-archive ``backup_<timestamp>/`` directories.

These directories are the only copy of this deployment's first six months, and every
property tested here is one that silently loses part of them if it regresses: a union
that prefers the wrong copy drops a transcript, a lexical sort of the group WAVs
scrambles the audio, and a conversation mapped without its ``data_purpose`` walks mined
speaker clips into the timeline.
"""

import json
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.legacy_backups import (
    LegacyChunk,
    discover_backups,
    load_corpus,
)

# The importer is a script, not a package module, so its pure helpers are reachable
# only by adding the scripts directory to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

from import_legacy_backups import (  # noqa: E402
    _conversation_document,
    _synthetic_chunks,
)

USER = "69b80e5894aa9ec334a421c9"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def init_db():
    """Beanie refuses to *construct* a Document before initialization, so the two
    mapping tests need a database even though they never write to one."""
    # 27018, matching conftest's ``mongo_service`` gate and every other Mongo test.
    # Dialling 27017 while the gate probes 27018 means the module only runs where
    # both ports have a server: CI publishes 27018 only, so it skipped the gate and
    # then failed to connect.
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    name = os.getenv("TEST_DB_NAME", "test_legacy_backups")
    await init_beanie(database=client[name], document_models=[Conversation])
    yield
    await client.drop_database(name)
    client.close()


def _write_backup(
    root: Path,
    stamp: str,
    *,
    conversations: list[dict],
    chunks: list[dict] | None = None,
    audio: dict[str, list[tuple[str, float]]] | None = None,
) -> Path:
    """One backup directory. ``audio`` maps conversation id -> [(filename, seconds)]."""
    path = root / f"backup_{stamp}"
    path.mkdir(parents=True)
    (path / "conversations.json").write_text(json.dumps(conversations))
    (path / "audio_chunks_metadata.json").write_text(json.dumps(chunks or []))
    for conversation_id, files in (audio or {}).items():
        directory = path / "audio" / conversation_id
        directory.mkdir(parents=True)
        for filename, seconds in files:
            with wave.open(str(directory / filename), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                # A per-file constant sample, so a scrambled concatenation is visible
                # in the bytes rather than only in the total length.
                marker = int(seconds * 100).to_bytes(2, "little", signed=True)
                handle.writeframes(marker * int(16000 * seconds))
    return path


def _conversation(conversation_id: str, **overrides) -> dict:
    row = {
        "conversation_id": conversation_id,
        "user_id": USER,
        "client_id": "a421c9-phone",
        "created_at": "2026-03-16T14:06:21.349000",
        "deleted": False,
        "transcript_versions": [],
        "active_transcript_version": None,
        "audio_chunks_count": 0,
        # View-only fields the API dump carries and the model has no room for.
        "id": "69b80e5d1851eb37349f257e",
        "transcript": None,
        "segments": [],
        "memory_count": 0,
    }
    row.update(overrides)
    return row


def _version(text: str) -> dict:
    return {
        "version_id": "v1",
        "transcript": text,
        "segments": [{"start": 0.0, "end": 1.0, "text": text, "speaker": "speaker_0"}],
        "words": [],
        "provider": "deepgram",
        "model": "nova-3",
        "created_at": "2026-03-16T15:15:19.471000",
    }


def test_discovers_backups_oldest_first(tmp_path: Path) -> None:
    _write_backup(tmp_path, "20260612_104458", conversations=[])
    _write_backup(tmp_path, "20260224_162251", conversations=[])
    (tmp_path / "not-a-backup").mkdir()

    found = discover_backups(tmp_path)

    assert [backup.name for backup in found] == [
        "backup_20260224_162251",
        "backup_20260612_104458",
    ]
    assert found[0].timestamp == datetime(2026, 2, 24, 16, 22, 51, tzinfo=timezone.utc)


def test_unions_a_conversation_split_across_backups(tmp_path: Path) -> None:
    """Transcript in the old backup, audio in the new one — both must survive.

    This is the whole reason the reader exists: conversations were deleted between
    backup runs, so no single directory holds a complete record.
    """
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000001"
    _write_backup(
        tmp_path,
        "20260224_162251",
        conversations=[
            _conversation(
                conversation_id,
                transcript_versions=[_version("hello from february")],
                active_transcript_version="v1",
            )
        ],
    )
    _write_backup(
        tmp_path,
        "20260612_104458",
        conversations=[_conversation(conversation_id)],
        chunks=[
            {
                "conversation_id": conversation_id,
                "chunk_index": 0,
                "start_time": 0.0,
                "end_time": 1.0,
                "duration": 1.0,
                "original_size": 32000,
                "compressed_size": 2800,
                "sample_rate": 16000,
                "channels": 1,
                "created_at": "2026-06-12 10:44:58.000000",
            }
        ],
        audio={conversation_id: [("chunk_001.wav", 1.0)]},
    )

    corpus = load_corpus(tmp_path)
    record = corpus.conversations[conversation_id]

    assert record.transcript == "hello from february"
    assert record.document_backup == "backup_20260224_162251"
    assert record.has_audio
    assert record.audio_backup == "backup_20260612_104458"
    assert len(record.chunks) == 1


def test_newer_record_wins_when_it_still_has_the_transcript(tmp_path: Path) -> None:
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000002"
    for stamp, text in (
        ("20260224_162251", "undiarized first pass"),
        ("20260612_104458", "diarized second pass"),
    ):
        _write_backup(
            tmp_path,
            stamp,
            conversations=[
                _conversation(
                    conversation_id,
                    transcript_versions=[_version(text)],
                    active_transcript_version="v1",
                )
            ],
        )

    corpus = load_corpus(tmp_path)

    assert corpus.conversations[conversation_id].transcript == "diarized second pass"


def test_group_wavs_concatenate_in_numeric_order(tmp_path: Path) -> None:
    """``chunk_10.wav`` follows ``chunk_9.wav``; a lexical sort puts it second."""
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000003"
    files = [(f"chunk_{index}.wav", float(index)) for index in range(1, 11)]
    _write_backup(
        tmp_path,
        "20260612_104458",
        conversations=[_conversation(conversation_id)],
        audio={conversation_id: files},
    )

    record = load_corpus(tmp_path).conversations[conversation_id]
    assert [path.name for path in record.audio_paths()] == [name for name, _ in files]

    pcm, sample_rate, channels = record.read_pcm()
    assert (sample_rate, channels) == (16000, 1)
    assert len(pcm) == int(sum(seconds for _, seconds in files) * 16000 * 2)
    # First sample belongs to chunk_1, last to chunk_10 — proves the order end to end.
    assert int.from_bytes(pcm[:2], "little", signed=True) == 100
    assert int.from_bytes(pcm[-2:], "little", signed=True) == 1000


def test_larger_audio_directory_wins(tmp_path: Path) -> None:
    """A later backup holding a truncated copy must not displace the full one."""
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000004"
    _write_backup(
        tmp_path,
        "20260612_104458",
        conversations=[_conversation(conversation_id)],
        audio={conversation_id: [("chunk_1.wav", 5.0), ("chunk_2.wav", 5.0)]},
    )
    _write_backup(
        tmp_path,
        "20260719_115823",
        conversations=[_conversation(conversation_id)],
        audio={conversation_id: [("chunk_1.wav", 1.0)]},
    )

    record = load_corpus(tmp_path).conversations[conversation_id]

    assert record.audio_backup == "backup_20260612_104458"
    assert len(record.read_pcm()[0]) == int(10.0 * 16000 * 2)


def test_synthetic_chunks_cover_audio_with_no_metadata(tmp_path: Path) -> None:
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000005"
    _write_backup(
        tmp_path, "20260612_104458", conversations=[_conversation(conversation_id)]
    )
    record = load_corpus(tmp_path).conversations[conversation_id]

    chunks = _synthetic_chunks(record, int(25.5 * 16000 * 2), 16000, 1)

    assert [chunk.duration for chunk in chunks] == [10.0, 10.0, pytest.approx(5.5)]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    assert chunks[-1].end_time == pytest.approx(25.5)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("mongo_service", "init_db")
async def test_mined_clips_are_marked_as_annotation(tmp_path: Path) -> None:
    """Without this they become episodes on the day they were mined, not recorded."""
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000006"
    _write_backup(
        tmp_path,
        "20260612_104458",
        conversations=[
            _conversation(conversation_id, client_id="a421c9-speaker-mining")
        ],
    )
    record = load_corpus(tmp_path).conversations[conversation_id]

    conversation = _conversation_document(record, USER, audio_present=True)

    assert conversation.data_purpose == "annotation"
    assert conversation.memory_excluded is True


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("mongo_service", "init_db")
async def test_mapping_drops_view_fields_and_flags_missing_audio(
    tmp_path: Path,
) -> None:
    conversation_id = "aaaaaaaa-0000-4000-8000-000000000007"
    _write_backup(
        tmp_path,
        "20260612_104458",
        conversations=[
            _conversation(
                conversation_id,
                audio_chunks_count=117,
                transcript_versions=[_version("survived without its audio")],
                active_transcript_version="v1",
            )
        ],
    )
    record = load_corpus(tmp_path).conversations[conversation_id]

    conversation = _conversation_document(record, USER, audio_present=False)

    assert conversation.conversation_id == conversation_id
    assert (
        conversation.transcript_versions[0].transcript == "survived without its audio"
    )
    # The API dump's ``id`` must not become the Mongo _id of the new document.
    assert conversation.id is None
    assert conversation.audio_archived is True
    assert conversation.archive_reason == "legacy_backup_audio_missing"
    assert conversation.audio_chunks_count == 0


def test_chunk_created_at_is_parsed_but_not_treated_as_capture_time() -> None:
    """The dumps store a naive write time; it is UTC and it is not an anchor."""
    chunk = LegacyChunk.from_row(
        {
            "conversation_id": "x",
            "chunk_index": 3,
            "start_time": 30.0,
            "end_time": 40.0,
            "duration": 10.0,
            "original_size": 320000,
            "compressed_size": 27559,
            "sample_rate": 16000,
            "channels": 1,
            "created_at": "2026-03-16 14:06:31.227000",
        }
    )

    assert chunk.created_at == datetime(
        2026, 3, 16, 14, 6, 31, 227000, tzinfo=timezone.utc
    )
    assert chunk.start_time == 30.0
