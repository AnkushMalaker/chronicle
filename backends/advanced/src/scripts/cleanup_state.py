#!/usr/bin/env python3
"""
Chronicle Backend Cleanup & Backup Tool

Features:
- Rich terminal UI with progress bars, panels, and colored output
- Backup-only mode (no cleanup)
- Strict backup verification before cleanup proceeds
- Conversation-filtered audio export (only conversations with transcripts)
- Comprehensive backup manifest with checksums
- Per-user vault backup (conversation_docs + memory_md tarred into vault.tar.gz)
- MongoDB, Redis, and vault cleanup (Syncthing markers preserved)
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import sys
import tarfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import redis
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.prompt import Confirm
    from rich.table import Table
    from rich.text import Text
    from rq import Queue

    from advanced_omi_backend.models.annotation import Annotation
    from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.models.user import User
    from advanced_omi_backend.models.waveform import WaveformData
    from advanced_omi_backend.utils.audio_chunk_utils import decode_opus_to_pcm
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("This script must be run inside the chronicle-backend container")
    sys.exit(1)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# Per-user markdown vaults on disk. Dirs are named by the user's 24-hex
# Mongo ObjectId; anything else (bench-*, agenttest, ...) is test data
# that backup/cleanup must not touch.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
VAULT_BASE_DIRS = ("conversation_docs", "memory_md")
_USER_ID_RE = re.compile(r"^[0-9a-f]{24}$")
# Syncthing folder markers — deleting these breaks the sync pairing
_SYNCTHING_MARKERS = (".stfolder", ".stignore")


def _iter_user_vaults():
    """Yield per-user vault directories under conversation_docs/ and memory_md/."""
    for base_name in VAULT_BASE_DIRS:
        base = DATA_DIR / base_name
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and _USER_ID_RE.fullmatch(d.name):
                yield d


def _count_vault_files() -> int:
    """Count vault content files, excluding Syncthing markers."""
    count = 0
    for vault in _iter_user_vaults():
        for f in vault.rglob("*"):
            if not f.is_file():
                continue
            rel_top = f.relative_to(vault).parts[0]
            if rel_top in _SYNCTHING_MARKERS:
                continue
            count += 1
    return count


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class Stats:
    """Track counts across the system."""

    def __init__(self):
        self.conversations = 0
        self.conversations_with_transcript = 0
        self.audio_chunks = 0
        self.waveforms = 0
        self.chat_sessions = 0
        self.chat_messages = 0
        self.annotations = 0
        self.redis_jobs = 0
        self.legacy_wav = 0
        self.vault_files = 0
        self.users = 0
        self.langfuse_prompts = 0


async def gather_stats(
    mongo_db: Any,
    redis_conn: Any,
    langfuse_client: Any = None,
) -> Stats:
    """Gather current system statistics."""
    s = Stats()

    # MongoDB
    s.conversations = await Conversation.find_all().count()
    s.conversations_with_transcript = await Conversation.find(
        Conversation.active_transcript_version != None  # noqa: E711
    ).count()
    s.audio_chunks = await mongo_db["audio_chunks"].count_documents({})
    s.waveforms = await WaveformData.find_all().count()
    s.chat_sessions = await mongo_db["chat_sessions"].count_documents({})
    s.chat_messages = await mongo_db["chat_messages"].count_documents({})
    s.annotations = await Annotation.find_all().count()
    s.users = await User.find_all().count()

    # Redis
    try:
        for qname in ("transcription", "memory", "audio", "default"):
            q = Queue(qname, connection=redis_conn)
            s.redis_jobs += (
                len(q)
                + len(q.started_job_registry)
                + len(q.finished_job_registry)
                + len(q.failed_job_registry)
                + len(q.canceled_job_registry)
                + len(q.deferred_job_registry)
                + len(q.scheduled_job_registry)
            )
    except Exception:
        pass

    # LangFuse prompts
    if langfuse_client:
        try:
            prompts_response = langfuse_client.api.prompts.list(limit=100)
            s.langfuse_prompts = (
                len(prompts_response.data) if hasattr(prompts_response, "data") else 0
            )
        except Exception:
            pass

    # Legacy WAV
    wav_dir = Path("/app/data/audio_chunks")
    if wav_dir.exists():
        s.legacy_wav = len(list(wav_dir.glob("*.wav")))

    # Vault (per-user markdown)
    s.vault_files = _count_vault_files()

    return s


def render_stats_table(stats: Stats, title: str = "Current State") -> Table:
    """Build a rich Table from Stats."""
    table = Table(
        title=title,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title_style="bold white",
        padding=(0, 2),
    )
    table.add_column("Category", style="white", min_width=24)
    table.add_column("Count", justify="right", style="bold", min_width=10)

    def row(label, value, style="white"):
        table.add_row(label, f"[{style}]{value}[/{style}]")

    row(
        "Conversations",
        str(stats.conversations),
        "green" if stats.conversations else "dim",
    )
    row(
        "  with transcripts",
        str(stats.conversations_with_transcript),
        "green" if stats.conversations_with_transcript else "dim",
    )
    row(
        "Audio Chunks",
        str(stats.audio_chunks),
        "green" if stats.audio_chunks else "dim",
    )
    row("Waveforms", str(stats.waveforms), "dim")
    row("Chat Sessions", str(stats.chat_sessions), "dim")
    row("Chat Messages", str(stats.chat_messages), "dim")
    row("Annotations", str(stats.annotations), "green" if stats.annotations else "dim")
    table.add_section()
    row(
        "LangFuse Prompts",
        str(stats.langfuse_prompts),
        "yellow" if stats.langfuse_prompts else "dim",
    )
    table.add_section()
    row("Redis Jobs", str(stats.redis_jobs), "dim")
    row("Legacy WAV Files", str(stats.legacy_wav), "dim")
    row("Vault Files", str(stats.vault_files), "green" if stats.vault_files else "dim")
    table.add_section()
    row("Users", str(stats.users), "cyan")

    return table


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class BackupResult:
    """Track which backup exports succeeded or failed."""

    def __init__(self):
        self.exports: dict[str, dict] = {}  # name -> {ok, path, size, sha256, error}

    def record(self, name: str, path: Optional[Path], ok: bool, error: str = ""):
        entry = {
            "ok": ok,
            "error": error,
            "path": str(path) if path else None,
            "size": 0,
            "sha256": "",
        }
        if ok and path and path.exists():
            if path.is_dir():
                # Directory export (e.g. audio WAVs): total size, no checksum
                entry["size"] = sum(
                    f.stat().st_size for f in path.rglob("*") if f.is_file()
                )
            else:
                entry["size"] = path.stat().st_size
                entry["sha256"] = _file_sha256(path)
        self.exports[name] = entry

    @property
    def all_ok(self) -> bool:
        return all(e["ok"] for e in self.exports.values())

    @property
    def critical_ok(self) -> bool:
        """conversations, audio_metadata, annotations, and vault are critical."""
        critical = ("conversations", "audio_metadata", "annotations", "vault")
        return all(
            self.exports.get(n, {}).get("ok", False)
            for n in critical
            if n in self.exports
        )

    def render_table(self) -> Table:
        table = Table(
            title="Backup Verification", border_style="dim", title_style="bold white"
        )
        table.add_column("Export", style="white", min_width=24)
        table.add_column("Status", justify="center", min_width=8)
        table.add_column("Size", justify="right", min_width=10)
        table.add_column("SHA-256", style="dim", min_width=12)

        for name, info in self.exports.items():
            if info["ok"]:
                status = "[green]OK[/green]"
                size = _human_size(info["size"])
                sha = info["sha256"][:12] + "..." if info["sha256"] else ""
            else:
                status = "[red]FAILED[/red]"
                size = "-"
                sha = info.get("error", "")[:30]
            table.add_row(name, status, size, sha)

        return table

    @property
    def total_size(self) -> int:
        return sum(e["size"] for e in self.exports.values())


class BackupManager:
    """Export data to a timestamped backup directory."""

    def __init__(
        self,
        backup_dir: str,
        export_audio: bool,
        mongo_db: Any,
        langfuse_client: Any = None,
        skip_existing_audio: bool = False,
    ):
        self.backup_dir = Path(backup_dir)
        self.export_audio = export_audio
        self.skip_existing_audio = skip_existing_audio
        self.mongo_db = mongo_db
        self.langfuse_client = langfuse_client
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.backup_path = self.backup_dir / f"backup_{self.timestamp}"

    async def run(
        self,
        stats: Stats,
    ) -> BackupResult:
        """Run all backup exports, return a BackupResult for verification."""
        self.backup_path.mkdir(parents=True, exist_ok=True)
        result = BackupResult()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            steps = [
                ("conversations", self._export_conversations),
                ("audio_metadata", self._export_audio_metadata),
                ("waveforms", self._export_waveforms),
                ("chat_sessions", self._export_chat_sessions),
                ("chat_messages", self._export_chat_messages),
                ("annotations", self._export_annotations),
                ("vault", self._export_vault),
            ]

            if self.export_audio:
                steps.append(("audio_wav", self._export_audio_wav))

            if self.langfuse_client:
                steps.append(("langfuse_prompts", self._export_langfuse_prompts))

            task = progress.add_task("Backing up...", total=len(steps))

            for name, func in steps:
                progress.update(task, description=f"Exporting {name}...")
                try:
                    ret = func(result)
                    path = await ret if asyncio.iscoroutine(ret) else ret
                    if not result.exports.get(name):
                        # func didn't record itself - record success
                        result.record(name, path, True)
                except Exception as e:
                    logger.warning(f"Export {name} failed: {e}")
                    result.record(name, None, False, str(e))
                progress.advance(task)

        # Write manifest
        manifest = {
            "timestamp": self.timestamp,
            "backup_path": str(self.backup_path),
            "exports": result.exports,
            "total_size_bytes": result.total_size,
            "total_size_human": _human_size(result.total_size),
            "stats": {
                "conversations": stats.conversations,
                "conversations_with_transcript": stats.conversations_with_transcript,
                "audio_chunks": stats.audio_chunks,
                "annotations": stats.annotations,
                "vault_files": stats.vault_files,
                "langfuse_prompts": stats.langfuse_prompts,
                "users": stats.users,
            },
        }
        manifest_path = self.backup_path / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)

        return result

    # -- Individual exports --------------------------------------------------

    async def _export_conversations(self, result: BackupResult) -> Path:
        conversations = await Conversation.find_all().to_list()
        data = [c.model_dump(mode="json") for c in conversations]
        path = self.backup_path / "conversations.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("conversations", path, True)
        return path

    async def _export_audio_metadata(self, result: BackupResult) -> Path:
        collection = self.mongo_db["audio_chunks"]
        cursor = collection.find({})
        data = []
        async for chunk in cursor:
            data.append(
                {
                    "conversation_id": chunk.get("conversation_id"),
                    "chunk_index": chunk.get("chunk_index"),
                    "start_time": chunk.get("start_time"),
                    "end_time": chunk.get("end_time"),
                    "duration": chunk.get("duration"),
                    "original_size": chunk.get("original_size"),
                    "compressed_size": chunk.get("compressed_size"),
                    "sample_rate": chunk.get("sample_rate", 16000),
                    "channels": chunk.get("channels", 1),
                    "has_speech": chunk.get("has_speech"),
                    "created_at": str(chunk.get("created_at", "")),
                }
            )
        path = self.backup_path / "audio_chunks_metadata.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("audio_metadata", path, True)
        return path

    async def _export_waveforms(self, result: BackupResult) -> Path:
        waveforms = await WaveformData.find_all().to_list()
        data = [w.model_dump(mode="json") for w in waveforms]
        path = self.backup_path / "waveforms.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("waveforms", path, True)
        return path

    async def _export_chat_sessions(self, result: BackupResult) -> Path:
        collection = self.mongo_db["chat_sessions"]
        cursor = collection.find({})
        data = []
        async for session in cursor:
            data.append(
                {
                    "session_id": session.get("session_id"),
                    "user_id": session.get("user_id"),
                    "title": session.get("title"),
                    "created_at": str(session.get("created_at", "")),
                    "updated_at": str(session.get("updated_at", "")),
                    "metadata": session.get("metadata", {}),
                }
            )
        path = self.backup_path / "chat_sessions.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("chat_sessions", path, True)
        return path

    async def _export_chat_messages(self, result: BackupResult) -> Path:
        collection = self.mongo_db["chat_messages"]
        cursor = collection.find({})
        data = []
        async for msg in cursor:
            data.append(
                {
                    "message_id": msg.get("message_id"),
                    "session_id": msg.get("session_id"),
                    "user_id": msg.get("user_id"),
                    "role": msg.get("role"),
                    "content": msg.get("content"),
                    "timestamp": str(msg.get("timestamp", "")),
                    "memories_used": msg.get("memories_used", []),
                    "metadata": msg.get("metadata", {}),
                }
            )
        path = self.backup_path / "chat_messages.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("chat_messages", path, True)
        return path

    async def _export_annotations(self, result: BackupResult) -> Path:
        annotations = await Annotation.find_all().to_list()
        data = [a.model_dump(mode="json") for a in annotations]
        path = self.backup_path / "annotations.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        result.record("annotations", path, True)
        return path

    def _export_vault(self, result: BackupResult) -> Path:
        """Tar per-user markdown vaults (conversation_docs + memory_md)."""
        path = self.backup_path / "vault.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for vault in _iter_user_vaults():
                tar.add(vault, arcname=str(vault.relative_to(DATA_DIR)))
        result.record("vault", path, True)
        return path

    def _find_previously_exported_audio(self) -> set[str]:
        """Conversation IDs whose audio already exists in a prior backup dir."""
        existing: set[str] = set()
        for conv_dir in self.backup_dir.glob("backup_*/audio/*"):
            if (
                conv_dir.is_dir()
                and conv_dir.parent.parent != self.backup_path
                and any(conv_dir.glob("*.wav"))
            ):
                existing.add(conv_dir.name)
        return existing

    async def _export_audio_wav(self, result: BackupResult) -> Optional[Path]:
        """Export audio WAV files for conversations that have transcripts."""
        # Only export audio for conversations with actual transcripts
        conversations = await Conversation.find(
            Conversation.active_transcript_version != None  # noqa: E711
        ).to_list()

        if not conversations:
            result.record("audio_wav", None, True)
            return None

        skip_ids: set[str] = set()
        if self.skip_existing_audio:
            skip_ids = self._find_previously_exported_audio()

        audio_dir = self.backup_path / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        exported = 0
        skipped = 0
        failed = 0

        for conv in conversations:
            if conv.conversation_id in skip_ids:
                skipped += 1
                continue
            try:
                ok = await self._export_conversation_audio(
                    conv.conversation_id, audio_dir
                )
                if ok:
                    exported += 1
            except Exception as e:
                logger.warning(f"Audio export failed for {conv.conversation_id}: {e}")
                failed += 1

        ok = exported > 0 or skipped > 0 or (len(conversations) == 0)
        notes = []
        if skipped:
            notes.append(f"{skipped} skipped (in prior backups)")
        if failed:
            notes.append(f"{failed} failed")
        result.record("audio_wav", audio_dir, ok, ", ".join(notes))
        return audio_dir

    async def _export_conversation_audio(
        self, conversation_id: str, audio_dir: Path
    ) -> bool:
        """Decode Opus chunks to 1-minute WAV files for a single conversation.

        Streams chunk-by-chunk so memory stays bounded (one opus chunk plus at
        most one minute of PCM), even for multi-hour conversations.
        Returns True if audio was exported.
        """
        cursor = AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id
        ).sort("+chunk_index")

        final_dir = audio_dir / conversation_id
        # Write into a temp dir and rename on completion, so an interrupted
        # export never leaves a partial dir that --skip-existing-audio would skip
        conv_dir = audio_dir / f"{conversation_id}.partial"
        sample_rate = None
        channels = None
        bytes_per_minute = 0
        pcm_buffer = bytearray()
        chunk_num = 1

        def _write_segment(segment_pcm: bytes):
            nonlocal chunk_num
            wav_path = conv_dir / f"chunk_{chunk_num:03d}.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(segment_pcm)
            chunk_num += 1

        async for chunk in cursor:
            if sample_rate is None:
                sample_rate = chunk.sample_rate
                channels = chunk.channels
                bytes_per_minute = (
                    sample_rate * channels * 2 * 60
                )  # 16-bit = 2 bytes per sample
                conv_dir.mkdir(parents=True, exist_ok=True)
            try:
                pcm_data = await decode_opus_to_pcm(
                    opus_data=bytes(chunk.audio_data),
                    sample_rate=sample_rate,
                    channels=channels,
                )
                pcm_buffer.extend(pcm_data)
            except Exception as e:
                logger.warning(
                    f"Opus decode error for {conversation_id} chunk {chunk.chunk_index}: {e}"
                )
                continue

            while len(pcm_buffer) >= bytes_per_minute:
                _write_segment(bytes(pcm_buffer[:bytes_per_minute]))
                del pcm_buffer[:bytes_per_minute]

        if pcm_buffer:
            _write_segment(bytes(pcm_buffer))

        if chunk_num > 1:
            conv_dir.rename(final_dir)
            return True
        if conv_dir.exists():
            conv_dir.rmdir()
        return False

    def _export_langfuse_prompts(self, result: BackupResult) -> Path:
        """Export all LangFuse prompts (production versions) including admin edits."""
        path = self.backup_path / "langfuse_prompts.json"
        data = []

        try:
            # Discover all prompt names via list API
            prompt_names = []
            prompts_response = self.langfuse_client.api.prompts.list(limit=100)
            if hasattr(prompts_response, "data"):
                for p in prompts_response.data:
                    prompt_names.append(p.name)

            # Fetch each prompt's production version with full text
            for name in prompt_names:
                try:
                    prompt_obj = self.langfuse_client.get_prompt(name)
                    entry = {
                        "name": name,
                        "prompt": prompt_obj.prompt,
                        "version": prompt_obj.version,
                    }
                    if hasattr(prompt_obj, "labels"):
                        entry["labels"] = prompt_obj.labels
                    if hasattr(prompt_obj, "config") and prompt_obj.config:
                        entry["config"] = prompt_obj.config
                    data.append(entry)
                except Exception as e:
                    logger.warning(f"Failed to fetch prompt '{name}': {e}")
                    data.append({"name": name, "error": str(e)})

        except Exception as e:
            result.record("langfuse_prompts", None, False, str(e))
            return path

        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        result.record("langfuse_prompts", path, True)
        return path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class CleanupManager:
    """Delete data across all services."""

    def __init__(
        self,
        mongo_db: Any,
        redis_conn: Any,
        include_wav: bool,
        delete_users: bool,
    ):
        self.mongo_db = mongo_db
        self.redis_conn = redis_conn
        self.include_wav = include_wav
        self.delete_users = delete_users

    async def run(self, stats: Stats) -> bool:
        """Perform cleanup with progress display."""
        steps = [
            ("MongoDB collections", self._cleanup_mongodb),
        ]
        steps.append(("Redis queues", self._cleanup_redis))
        steps.append(("Vault markdown", self._cleanup_vault))
        if self.include_wav:
            steps.append(("Legacy WAV files", self._cleanup_legacy_wav))

        ok = True
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Cleaning...", total=len(steps))
            for label, func in steps:
                progress.update(task, description=f"Cleaning {label}...")
                try:
                    if asyncio.iscoroutinefunction(func):
                        await func(stats)
                    else:
                        func(stats)
                except Exception as e:
                    logger.error(f"Failed to clean {label}: {e}")
                    ok = False
                progress.advance(task)

        return ok

    async def _cleanup_mongodb(self, stats: Stats):
        await Conversation.find_all().delete()
        await self.mongo_db["audio_chunks"].delete_many({})
        await WaveformData.find_all().delete()
        await self.mongo_db["chat_sessions"].delete_many({})
        await self.mongo_db["chat_messages"].delete_many({})
        await Annotation.find_all().delete()
        if self.delete_users:
            await User.find_all().delete()

    def _cleanup_redis(self, stats: Stats):
        for qname in ("transcription", "memory", "audio", "default"):
            try:
                q = Queue(qname, connection=self.redis_conn)
                q.empty()
                for registry in (
                    q.started_job_registry,
                    q.finished_job_registry,
                    q.failed_job_registry,
                    q.canceled_job_registry,
                    q.deferred_job_registry,
                    q.scheduled_job_registry,
                ):
                    for job_id in registry.get_job_ids():
                        registry.remove(job_id)
            except Exception as e:
                logger.warning(f"Redis queue {qname} cleanup failed: {e}")

    def _cleanup_vault(self, stats: Stats):
        """Clear per-user vault contents, preserving Syncthing markers.

        Keeping .stfolder/.stignore keeps the Syncthing pairing intact;
        the deletions propagate to paired remote vaults (e.g. Obsidian).
        """
        for vault in _iter_user_vaults():
            for entry in vault.iterdir():
                if entry.name in _SYNCTHING_MARKERS:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()

    def _cleanup_legacy_wav(self, stats: Stats):
        wav_dir = Path("/app/data/audio_chunks")
        if not wav_dir.exists():
            return
        for f in wav_dir.glob("*.wav"):
            f.unlink()


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------


async def connect_services():
    """Initialize all service connections. Returns (mongo_db, redis_conn, langfuse_client)."""
    # MongoDB
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
    mongodb_database = os.getenv("MONGODB_DATABASE", "chronicle")
    mongo_client = AsyncIOMotorClient(mongodb_uri)
    mongo_db = mongo_client[mongodb_database]
    await init_beanie(
        database=mongo_db,
        document_models=[
            Conversation,
            AudioChunkDocument,
            WaveformData,
            User,
            Annotation,
        ],
    )

    # Redis
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis_conn = redis.from_url(redis_url)

    # LangFuse
    langfuse_client = None
    langfuse_host = os.getenv("LANGFUSE_HOST")
    langfuse_public = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret = os.getenv("LANGFUSE_SECRET_KEY")
    if langfuse_host and langfuse_public and langfuse_secret:
        try:
            from langfuse import Langfuse

            langfuse_client = Langfuse()
        except Exception:
            langfuse_client = None

    return mongo_db, redis_conn, langfuse_client


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def print_header():
    console.print()
    console.print(
        Panel(
            Text("Chronicle Cleanup & Backup", style="bold white", justify="center"),
            subtitle=datetime.now().strftime("%Y-%m-%d %H:%M"),
            border_style="blue",
            padding=(1, 4),
        )
    )


def print_dry_run(stats: Stats, args):
    console.print()
    console.print(
        Panel(
            "[bold yellow]DRY-RUN MODE[/bold yellow] - no changes will be made",
            border_style="yellow",
        )
    )
    console.print()

    if args.backup or args.backup_only:
        console.print(
            "[cyan]Would create backup at:[/cyan]",
            str(Path(args.backup_dir) / f"backup_..."),
        )
        if args.export_audio:
            audio_note = f"(from {stats.conversations_with_transcript} conversations with transcripts)"
            console.print(f"[cyan]Would export audio WAV files[/cyan] {audio_note}")
        console.print()

    if not args.backup_only:
        table = Table(title="Would Delete", border_style="red", title_style="bold red")
        table.add_column("Data", style="white")
        table.add_column("Count", justify="right", style="bold red")

        table.add_row("Conversations", str(stats.conversations))
        table.add_row("Audio Chunks", str(stats.audio_chunks))
        table.add_row("Waveforms", str(stats.waveforms))
        table.add_row("Chat Sessions", str(stats.chat_sessions))
        table.add_row("Chat Messages", str(stats.chat_messages))
        table.add_row("Annotations", str(stats.annotations))
        table.add_row("Redis Jobs", str(stats.redis_jobs))
        table.add_row("Vault Files", str(stats.vault_files))
        if args.include_wav:
            table.add_row("Legacy WAV Files", str(stats.legacy_wav))
        if args.delete_users:
            table.add_row("[bold red]Users (DANGEROUS)[/bold red]", str(stats.users))
        else:
            table.add_row("[green]Users (preserved)[/green]", str(stats.users))

        console.print(table)
    else:
        console.print("[green]Backup-only mode[/green] - no data would be deleted")

    console.print()
    console.print("[dim]Run without --dry-run to proceed[/dim]")


def print_confirmation(stats: Stats, args) -> bool:
    """Show what will happen and ask for confirmation. Returns True if user confirms."""
    console.print()

    if args.backup or args.backup_only:
        console.print(
            Panel(
                f"[green]Backup will be created at:[/green] {args.backup_dir}\n"
                + (
                    "[green]Audio WAV export included[/green]"
                    if args.export_audio
                    else "[dim]Audio WAV export: off[/dim]"
                ),
                title="Backup",
                border_style="green",
            )
        )
    elif not args.backup_only:
        console.print(
            Panel(
                "[bold red]No backup will be created![/bold red]\nData will be permanently lost.",
                title="Warning",
                border_style="red",
            )
        )

    if not args.backup_only:
        items = [
            f"  {stats.conversations} conversations",
            f"  {stats.audio_chunks} audio chunks",
            f"  {stats.waveforms} waveforms",
            f"  {stats.chat_sessions} chat sessions",
            f"  {stats.chat_messages} chat messages",
            f"  {stats.annotations} annotations",
        ]
        items.append(f"  {stats.redis_jobs} Redis jobs")
        items.append(
            f"  {stats.vault_files} vault files (deletions sync to paired Obsidian vaults)"
        )
        if args.include_wav:
            items.append(f"  {stats.legacy_wav} legacy WAV files")
        if args.delete_users:
            items.append(f"  [bold red]{stats.users} users (DANGEROUS)[/bold red]")

        console.print(
            Panel(
                "\n".join(items),
                title="[bold red]Will Delete[/bold red]",
                border_style="red",
            )
        )

    console.print()
    return Confirm.ask("[bold]Proceed?[/bold]", default=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(
        description="Chronicle Cleanup & Backup Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./cleanup.sh --dry-run                    Preview what would happen
  ./cleanup.sh --backup-only                Back up everything (no cleanup)
  ./cleanup.sh --backup-only --export-audio Back up everything including audio WAV
  ./cleanup.sh --backup                     Back up then clean
  ./cleanup.sh --backup --export-audio      Back up with audio then clean
  ./cleanup.sh --backup --force             Skip confirmation prompt
        """,
    )

    parser.add_argument(
        "--backup", action="store_true", help="Create backup before cleaning"
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Create backup WITHOUT cleaning (safe)",
    )
    parser.add_argument(
        "--export-audio",
        action="store_true",
        help="Include audio WAV export in backup (conversations with transcripts only)",
    )
    parser.add_argument(
        "--skip-existing-audio",
        action="store_true",
        help="Skip audio export for conversations already present in prior backups",
    )
    parser.add_argument(
        "--include-wav", action="store_true", help="Include legacy WAV file cleanup"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without making changes"
    )
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument(
        "--backup-dir",
        type=str,
        default="/app/data/backups",
        help="Backup directory (default: /app/data/backups)",
    )
    parser.add_argument(
        "--delete-users",
        action="store_true",
        help="DANGEROUS: Also delete user accounts",
    )

    args = parser.parse_args()

    # Validation
    if args.export_audio and not (args.backup or args.backup_only):
        console.print("[red]--export-audio requires --backup or --backup-only[/red]")
        sys.exit(1)

    # Header
    print_header()

    # Connect
    with console.status("[bold cyan]Connecting to services...", spinner="dots"):
        mongo_db, redis_conn, langfuse_client = await connect_services()

    # Gather stats
    with console.status("[bold cyan]Gathering statistics...", spinner="dots"):
        stats = await gather_stats(mongo_db, redis_conn, langfuse_client)

    console.print()
    console.print(render_stats_table(stats, "Current Backend State"))
    console.print()

    # Dry-run
    if args.dry_run:
        print_dry_run(stats, args)
        return

    # Confirmation
    if not args.force:
        if not print_confirmation(stats, args):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    # Backup
    do_backup = args.backup or args.backup_only
    if do_backup:
        console.print()
        backup_mgr = BackupManager(
            args.backup_dir,
            args.export_audio,
            mongo_db,
            langfuse_client,
            skip_existing_audio=args.skip_existing_audio,
        )
        result = await backup_mgr.run(stats)

        console.print()
        console.print(result.render_table())
        console.print()
        console.print(
            f"[bold]Backup size:[/bold] {_human_size(result.total_size)}  "
            f"[bold]Location:[/bold] {backup_mgr.backup_path}"
        )

        if not result.critical_ok:
            console.print()
            console.print(
                Panel(
                    "[bold red]Critical backup exports failed![/bold red]\n"
                    "Conversations or audio metadata could not be exported.\n"
                    "Cleanup will NOT proceed to protect your data.",
                    title="Backup Verification Failed",
                    border_style="red",
                )
            )
            sys.exit(1)

        if not result.all_ok:
            console.print()
            console.print(
                "[yellow]Some non-critical exports failed (see table above).[/yellow]"
            )

    # If backup-only, we're done
    if args.backup_only:
        console.print()
        console.print(
            Panel(
                "[bold green]Backup completed successfully![/bold green]\n"
                "No data was deleted.",
                border_style="green",
            )
        )
        return

    # Cleanup
    console.print()
    cleanup_mgr = CleanupManager(
        mongo_db,
        redis_conn,
        args.include_wav,
        args.delete_users,
    )
    success = await cleanup_mgr.run(stats)

    if not success:
        console.print(
            Panel(
                "[bold red]Cleanup encountered errors![/bold red]", border_style="red"
            )
        )
        sys.exit(1)

    # Verify
    console.print()
    with console.status("[bold cyan]Verifying cleanup...", spinner="dots"):
        final_stats = await gather_stats(mongo_db, redis_conn, langfuse_client)

    console.print(render_stats_table(final_stats, "After Cleanup"))

    console.print()
    msg = "[bold green]Cleanup completed successfully![/bold green]"
    if do_backup:
        msg += f"\n[green]Backup saved to:[/green] {backup_mgr.backup_path}"
    console.print(Panel(msg, border_style="green"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Fatal error:[/bold red] {e}")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
