"""Read the pre-archive backup directories written by ``scripts/cleanup_state.py``.

Chronicle's durable backup format is the ``.chronicle`` archive (BSON collections plus
the filesystem, SHA-256 per member). Everything recorded before that shipped lives in
``data/backups/backup_<timestamp>/`` instead: JSON dumps of the *API* shape of each
collection, and audio decoded back to WAV. Those directories are the only surviving
record of this deployment's first six months, so they need a reader rather than a
migration note.

Two properties of that era make a naive read wrong:

**The backups are incremental in effect, not by design.** Each run dumped whatever the
database held *at that moment*, and conversations were deleted between runs. So a
conversation's transcript can be in one directory, its audio in an older one, and
neither in the newest. Anything asking "what do we still have" has to union across all
of them, per conversation, per artifact.

**The audio directory is not one file per chunk.** ``audio/<conversation_id>/`` holds
grouped WAVs — measured here, 60 seconds each, so six 10-second chunks per file — while
``audio_chunks_metadata.json`` describes the chunks. Concatenating the group files in
numeric order reproduces the conversation's PCM exactly (verified against the metadata's
own durations on 497 of 498 conversation directories), which is what makes re-slicing
into chunks safe.

This module only *reads*. It derives no capture times and writes nothing: see
``scripts/import_legacy_backups.py`` for ingestion and
``scripts/backfill_chunk_capture_time.py`` for the anchoring policy.
"""

from __future__ import annotations

import io
import json
import logging
import re
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

BACKUP_DIR_PATTERN = re.compile(r"^backup_(\d{8}_\d{6})$")
GROUP_WAV_PATTERN = re.compile(r"(\d+)")
# ``<epoch_ms>_<client_id>_<conversation_id>.wav`` — the whole-conversation WAVs the
# very first backends wrote. The epoch prefix is a real capture time and is the only
# place it survives, so it is parsed out rather than discarded with the filename.
LEGACY_WAV_PATTERN = re.compile(r"^(\d{13})_(.+)_([0-9a-f-]{36})\.wav$")


def _as_utc(value: datetime) -> datetime:
    """Legacy dumps are naive; every writer of them stored UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace(" ", "T")))
    except ValueError:
        return None


@dataclass(frozen=True)
class LegacyChunk:
    """One row of ``audio_chunks_metadata.json``.

    ``created_at`` is when the chunk was *written*, which is only capture time for a
    live stream keeping up with real time. Measured across one backup: 165 conversations
    advance ~10s per chunk (streaming), 4 were written in a burst (batch), and 200 tick
    at ~15.7s per 10s of audio — a writer running behind. So this is carried through for
    diagnosis and never used as an anchor.
    """

    conversation_id: str
    chunk_index: int
    start_time: float
    end_time: float
    duration: float
    original_size: int
    compressed_size: int
    sample_rate: int
    channels: int
    created_at: datetime | None
    has_speech: bool | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "LegacyChunk":
        return cls(
            conversation_id=str(row["conversation_id"]),
            chunk_index=int(row["chunk_index"]),
            start_time=float(row.get("start_time") or 0.0),
            end_time=float(row.get("end_time") or 0.0),
            duration=float(row.get("duration") or 0.0),
            original_size=int(row.get("original_size") or 0),
            compressed_size=int(row.get("compressed_size") or 0),
            sample_rate=int(row.get("sample_rate") or 16000),
            channels=int(row.get("channels") or 1),
            created_at=parse_timestamp(row.get("created_at")),
            has_speech=row.get("has_speech"),
        )


@dataclass
class LegacyBackup:
    """One ``backup_<timestamp>/`` directory."""

    path: Path
    timestamp: datetime

    @property
    def name(self) -> str:
        return self.path.name

    def _load_json(self, filename: str) -> Any:
        target = self.path / filename
        if not target.is_file() or target.stat().st_size <= 2:
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("%s/%s is not readable JSON", self.name, filename)
            return None

    def conversations(self) -> list[dict[str, Any]]:
        payload = self._load_json("conversations.json")
        return [row for row in (payload or []) if row.get("conversation_id")]

    def chunk_rows(self) -> list[dict[str, Any]]:
        payload = self._load_json("audio_chunks_metadata.json")
        return [row for row in (payload or []) if row.get("conversation_id")]

    def audio_dirs(self) -> dict[str, Path]:
        root = self.path / "audio"
        if not root.is_dir():
            return {}
        return {
            child.name: child
            for child in root.iterdir()
            if child.is_dir() and any(child.iterdir())
        }

    def legacy_wavs(self) -> dict[str, tuple[Path, datetime]]:
        root = self.path / "legacy_wav"
        if not root.is_dir():
            return {}
        found: dict[str, tuple[Path, datetime]] = {}
        for child in sorted(root.iterdir()):
            match = LEGACY_WAV_PATTERN.match(child.name)
            if not match:
                continue
            captured = datetime.fromtimestamp(int(match.group(1)) / 1000, timezone.utc)
            found[match.group(3)] = (child, captured)
        return found


@dataclass
class LegacyConversation:
    """One conversation, assembled from every backup that still holds part of it."""

    conversation_id: str
    document: dict[str, Any]
    document_backup: str
    chunks: list[LegacyChunk] = field(default_factory=list)
    chunks_backup: str | None = None
    audio_dir: Path | None = None
    audio_backup: str | None = None
    legacy_wav: Path | None = None
    legacy_wav_captured_at: datetime | None = None
    legacy_wav_backup: str | None = None
    seen_in: list[str] = field(default_factory=list)

    @property
    def created_at(self) -> datetime | None:
        return parse_timestamp(self.document.get("created_at"))

    @property
    def client_id(self) -> str:
        return str(self.document.get("client_id") or "")

    @property
    def user_id(self) -> str:
        return str(self.document.get("user_id") or "")

    @property
    def deleted(self) -> bool:
        return bool(self.document.get("deleted"))

    @property
    def transcript(self) -> str:
        """The active transcript, falling back to the conversation's flat copy."""
        version = self.active_version
        if version and (version.get("transcript") or "").strip():
            return str(version["transcript"]).strip()
        return str(self.document.get("transcript") or "").strip()

    @property
    def active_version(self) -> dict[str, Any] | None:
        versions = self.document.get("transcript_versions") or []
        active = self.document.get("active_transcript_version")
        for version in versions:
            if version.get("version_id") == active:
                return version
        return versions[-1] if versions else None

    @property
    def has_audio(self) -> bool:
        return self.audio_dir is not None or self.legacy_wav is not None

    @property
    def audio_duration(self) -> float:
        if self.chunks:
            return sum(chunk.duration for chunk in self.chunks)
        return float(self.document.get("audio_total_duration") or 0.0)

    def audio_paths(self) -> list[Path]:
        """The WAV files to concatenate, in playback order."""
        if self.audio_dir is not None:
            return sorted(
                (path for path in self.audio_dir.iterdir() if path.is_file()),
                key=_group_sort_key,
            )
        return [self.legacy_wav] if self.legacy_wav else []

    def read_pcm(self) -> tuple[bytes, int, int]:
        """Concatenated 16-bit PCM for the whole conversation, with its format."""
        buffer = io.BytesIO()
        sample_rate = 16000
        channels = 1
        for path in self.audio_paths():
            with wave.open(str(path), "rb") as handle:
                if handle.getsampwidth() != 2:
                    raise ValueError(f"{path} is not 16-bit PCM")
                sample_rate = handle.getframerate()
                channels = handle.getnchannels()
                buffer.write(handle.readframes(handle.getnframes()))
        return buffer.getvalue(), sample_rate, channels


def _group_sort_key(path: Path) -> tuple[int, str]:
    """``chunk_10.wav`` after ``chunk_9.wav``, which a lexical sort gets wrong."""
    digits = GROUP_WAV_PATTERN.findall(path.stem)
    return (int(digits[-1]) if digits else 0, path.name)


@dataclass
class LegacyCorpus:
    conversations: dict[str, LegacyConversation]
    backups: list[LegacyBackup]

    def __iter__(self) -> Iterator[LegacyConversation]:
        return iter(
            sorted(
                self.conversations.values(),
                key=lambda item: (
                    item.created_at or datetime.max.replace(tzinfo=timezone.utc),
                    item.conversation_id,
                ),
            )
        )

    def __len__(self) -> int:
        return len(self.conversations)


def discover_backups(root: Path) -> list[LegacyBackup]:
    """Every ``backup_<timestamp>/`` under ``root``, oldest first."""
    found: list[LegacyBackup] = []
    for child in sorted(root.iterdir() if root.is_dir() else []):
        match = BACKUP_DIR_PATTERN.match(child.name) if child.is_dir() else None
        if not match:
            continue
        found.append(
            LegacyBackup(
                path=child,
                timestamp=datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
                    tzinfo=timezone.utc
                ),
            )
        )
    return found


def _audio_bytes(path: Path) -> int:
    return sum(child.stat().st_size for child in path.iterdir() if child.is_file())


def _prefer_document(current: LegacyConversation, candidate: dict[str, Any]) -> bool:
    """Whether ``candidate`` should replace the record we already hold.

    A later dump of the same conversation carries later processing — diarization, a
    generated title, a corrected transcript — so newest wins, *except* that a record
    which lost its transcript must never displace one that still has it. Deliberately
    not "longest transcript wins": length is not quality, and a length race silently
    prefers an undiarized re-run over a diarized one.
    """
    incoming_has_text = bool(
        (candidate.get("transcript") or "").strip()
        or candidate.get("transcript_versions")
    )
    if bool(current.transcript) and not incoming_has_text:
        return False
    return True


def load_corpus(
    root: Path, *, backups: list[LegacyBackup] | None = None
) -> LegacyCorpus:
    """Union every backup under ``root`` into one record per conversation."""
    ordered = backups if backups is not None else discover_backups(root)
    conversations: dict[str, LegacyConversation] = {}

    for backup in ordered:
        for row in backup.conversations():
            conversation_id = str(row["conversation_id"])
            existing = conversations.get(conversation_id)
            if existing is None:
                conversations[conversation_id] = LegacyConversation(
                    conversation_id=conversation_id,
                    document=row,
                    document_backup=backup.name,
                    seen_in=[backup.name],
                )
                continue
            existing.seen_in.append(backup.name)
            if _prefer_document(existing, row):
                existing.document = row
                existing.document_backup = backup.name

        by_conversation: dict[str, list[LegacyChunk]] = {}
        for row in backup.chunk_rows():
            chunk = LegacyChunk.from_row(row)
            by_conversation.setdefault(chunk.conversation_id, []).append(chunk)
        for conversation_id, chunks in by_conversation.items():
            record = conversations.get(conversation_id)
            if record is None:
                continue
            # Most chunks wins, not newest: a later backup can hold a trimmed or
            # partially re-bound copy of the same recording.
            if len(chunks) >= len(record.chunks):
                record.chunks = sorted(chunks, key=lambda item: item.chunk_index)
                record.chunks_backup = backup.name

        for conversation_id, path in backup.audio_dirs().items():
            record = conversations.get(conversation_id)
            if record is None:
                continue
            if record.audio_dir is not None and _audio_bytes(path) <= _audio_bytes(
                record.audio_dir
            ):
                continue
            record.audio_dir = path
            record.audio_backup = backup.name

        for conversation_id, (path, captured) in backup.legacy_wavs().items():
            record = conversations.get(conversation_id)
            if record is None:
                continue
            record.legacy_wav = path
            record.legacy_wav_captured_at = captured
            record.legacy_wav_backup = backup.name

    return LegacyCorpus(conversations=conversations, backups=list(ordered))
