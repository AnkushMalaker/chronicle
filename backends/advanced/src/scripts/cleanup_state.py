#!/usr/bin/env python3
"""
Backend State Cleanup Script for Chronicle

This script provides comprehensive cleanup of Chronicle backend data including:
- MongoDB collections (conversations, audio_chunks)
- Qdrant vector store (memories)
- Redis job queues and registries
- Legacy WAV files (backward compatibility)

Features:
- Optional backup before cleanup (metadata and/or full audio export)
- Dry-run mode for safe preview
- User account preservation by default
- Confirmation prompts with detailed warnings
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import redis
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams
    from rq import Queue

    from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.models.user import User
    from advanced_omi_backend.models.waveform import WaveformData
    from advanced_omi_backend.services.memory.config import build_memory_config_from_env
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("This script must be run inside the chronicle-backend container")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_qdrant_collection_name() -> str:
    """Get Qdrant collection name from memory service configuration."""
    try:
        memory_config = build_memory_config_from_env()
        if hasattr(memory_config, 'vector_store_config') and memory_config.vector_store_config:
            collection_name = memory_config.vector_store_config.get('collection_name', 'chronicle_memories')
            logger.info(f"Using Qdrant collection name from config: {collection_name}")
            return collection_name
    except Exception as e:
        logger.warning(f"Could not load collection name from config: {e}")

    # Fallback to default
    logger.info("Using default Qdrant collection name: chronicle_memories")
    return "chronicle_memories"


class CleanupStats:
    """Track cleanup statistics"""
    def __init__(self):
        self.conversations_count = 0
        self.audio_chunks_count = 0
        self.waveforms_count = 0
        self.chat_sessions_count = 0
        self.chat_messages_count = 0
        self.memories_count = 0
        self.redis_jobs_count = 0
        self.legacy_wav_count = 0
        self.users_count = 0
        self.backup_size_bytes = 0
        self.backup_path = None


class BackupManager:
    """Handle backup operations"""

    def __init__(self, backup_dir: str, export_audio: bool, mongo_db: Any):
        self.backup_dir = Path(backup_dir)
        self.export_audio = export_audio
        self.mongo_db = mongo_db
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.backup_path = self.backup_dir / f"backup_{self.timestamp}"

    async def create_backup(
        self,
        qdrant_client: Optional[AsyncQdrantClient],
        stats: CleanupStats
    ) -> bool:
        """Create complete backup of all data"""
        try:
            logger.info(f"Creating backup at {self.backup_path}")
            self.backup_path.mkdir(parents=True, exist_ok=True)
            stats.backup_path = str(self.backup_path)

            # Export MongoDB data
            await self._export_conversations(stats)
            await self._export_audio_chunks_metadata(stats)
            await self._export_waveforms(stats)
            await self._export_chat_sessions(stats)
            await self._export_chat_messages(stats)

            # Export audio as WAV if requested
            if self.export_audio:
                await self._export_audio_wav(stats)

            # Export Qdrant vectors
            if qdrant_client:
                await self._export_memories(qdrant_client, stats)

            # Generate summary
            await self._generate_summary(stats)

            # Calculate backup size
            stats.backup_size_bytes = sum(
                f.stat().st_size for f in self.backup_path.rglob('*') if f.is_file()
            )

            logger.info(f"Backup completed: {stats.backup_size_bytes / (1024**2):.2f} MB")
            return True

        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            return False

    async def _export_conversations(self, stats: CleanupStats):
        """Export all conversations to JSON"""
        logger.info("Exporting conversations...")
        conversations = await Conversation.find_all().to_list()
        stats.conversations_count = len(conversations)

        # Serialize conversations (handle datetime, UUID, etc.)
        conversations_data = []
        for conv in conversations:
            conv_dict = conv.model_dump(mode='json')
            conversations_data.append(conv_dict)

        output_path = self.backup_path / "conversations.json"
        with open(output_path, 'w') as f:
            json.dump(conversations_data, f, indent=2, default=str)

        logger.info(f"Exported {stats.conversations_count} conversations")

    async def _export_audio_chunks_metadata(self, stats: CleanupStats):
        """Export audio chunks metadata (not the actual audio)"""
        logger.info("Exporting audio chunks metadata...")

        # Use raw MongoDB query to handle malformed documents
        # (some old/corrupted chunks may not validate against current schema)
        audio_chunks_collection = self.mongo_db["audio_chunks"]
        chunks_cursor = audio_chunks_collection.find({})

        chunks_data = []
        malformed_count = 0

        async for chunk in chunks_cursor:
            try:
                # Extract fields safely with defaults for missing values
                chunk_dict = {
                    'conversation_id': chunk.get('conversation_id'),
                    'chunk_index': chunk.get('chunk_index'),
                    'start_time': chunk.get('start_time'),
                    'end_time': chunk.get('end_time'),
                    'duration': chunk.get('duration'),
                    'original_size': chunk.get('original_size'),
                    'compressed_size': chunk.get('compressed_size'),
                    'sample_rate': chunk.get('sample_rate', 16000),
                    'channels': chunk.get('channels', 1),
                    'has_speech': chunk.get('has_speech'),
                    'created_at': str(chunk.get('created_at', ''))
                }
                chunks_data.append(chunk_dict)
            except Exception as e:
                malformed_count += 1
                logger.warning(f"Skipping malformed chunk {chunk.get('_id')}: {e}")
                continue

        stats.audio_chunks_count = len(chunks_data)

        output_path = self.backup_path / "audio_chunks_metadata.json"
        with open(output_path, 'w') as f:
            json.dump(chunks_data, f, indent=2, default=str)

        logger.info(f"Exported {stats.audio_chunks_count} audio chunks metadata")
        if malformed_count > 0:
            logger.warning(f"Skipped {malformed_count} malformed chunks")

    async def _export_waveforms(self, stats: CleanupStats):
        """Export waveform visualization data"""
        logger.info("Exporting waveforms...")

        waveforms = await WaveformData.find_all().to_list()
        stats.waveforms_count = len(waveforms)

        # Serialize waveforms
        waveforms_data = []
        for waveform in waveforms:
            waveform_dict = waveform.model_dump(mode='json')
            waveforms_data.append(waveform_dict)

        output_path = self.backup_path / "waveforms.json"
        with open(output_path, 'w') as f:
            json.dump(waveforms_data, f, indent=2, default=str)

        logger.info(f"Exported {stats.waveforms_count} waveforms")

    async def _export_chat_sessions(self, stats: CleanupStats):
        """Export chat sessions metadata"""
        logger.info("Exporting chat sessions...")

        chat_sessions_collection = self.mongo_db["chat_sessions"]
        sessions_cursor = chat_sessions_collection.find({})

        sessions_data = []
        async for session in sessions_cursor:
            session_dict = {
                'session_id': session.get('session_id'),
                'user_id': session.get('user_id'),
                'title': session.get('title'),
                'created_at': str(session.get('created_at', '')),
                'updated_at': str(session.get('updated_at', '')),
                'metadata': session.get('metadata', {})
            }
            sessions_data.append(session_dict)

        stats.chat_sessions_count = len(sessions_data)

        output_path = self.backup_path / "chat_sessions.json"
        with open(output_path, 'w') as f:
            json.dump(sessions_data, f, indent=2, default=str)

        logger.info(f"Exported {stats.chat_sessions_count} chat sessions")

    async def _export_chat_messages(self, stats: CleanupStats):
        """Export chat messages"""
        logger.info("Exporting chat messages...")

        chat_messages_collection = self.mongo_db["chat_messages"]
        messages_cursor = chat_messages_collection.find({})

        messages_data = []
        async for message in messages_cursor:
            message_dict = {
                'message_id': message.get('message_id'),
                'session_id': message.get('session_id'),
                'user_id': message.get('user_id'),
                'role': message.get('role'),
                'content': message.get('content'),
                'timestamp': str(message.get('timestamp', '')),
                'memories_used': message.get('memories_used', []),
                'metadata': message.get('metadata', {})
            }
            messages_data.append(message_dict)

        stats.chat_messages_count = len(messages_data)

        output_path = self.backup_path / "chat_messages.json"
        with open(output_path, 'w') as f:
            json.dump(messages_data, f, indent=2, default=str)

        logger.info(f"Exported {stats.chat_messages_count} chat messages")

    async def _export_audio_wav(self, stats: CleanupStats):
        """Export audio as WAV files (1-minute chunks)"""
        logger.info("Exporting audio as WAV files (this may take a while)...")

        # Get all unique conversation IDs
        conversations = await Conversation.find_all().to_list()
        audio_dir = self.backup_path / "audio"

        for conv in conversations:
            try:
                await self._export_conversation_audio(conv.conversation_id, audio_dir)
            except Exception as e:
                logger.warning(f"Failed to export audio for {conv.conversation_id}: {e}")
                continue

        logger.info("Audio export completed")

    async def _export_conversation_audio(self, conversation_id: str, audio_dir: Path):
        """Export audio for a single conversation as 1-minute WAV chunks"""
        # Get all chunks for this conversation
        chunks = await AudioChunkDocument.find(
            AudioChunkDocument.conversation_id == conversation_id
        ).sort("+chunk_index").to_list()

        if not chunks:
            return

        # Create conversation directory
        conv_dir = audio_dir / conversation_id
        conv_dir.mkdir(parents=True, exist_ok=True)

        # Decode all Opus chunks to PCM
        pcm_data = []
        sample_rate = chunks[0].sample_rate
        channels = chunks[0].channels

        try:
            import opuslib
            decoder = opuslib.Decoder(sample_rate, channels)

            for chunk in chunks:
                # Decode Opus to PCM
                # Note: frame_size depends on sample rate and duration
                frame_size = int(sample_rate * chunk.duration / channels)
                decoded = decoder.decode(bytes(chunk.audio_data), frame_size)
                pcm_data.append(decoded)

        except ImportError:
            logger.warning("opuslib not available, skipping audio export")
            return
        except Exception as e:
            logger.warning(f"Failed to decode audio for {conversation_id}: {e}")
            return

        # Concatenate all PCM data
        all_pcm = b''.join(pcm_data)

        # Convert bytes to int16 samples
        samples = struct.unpack(f'<{len(all_pcm)//2}h', all_pcm)

        # Split into 1-minute chunks
        samples_per_minute = sample_rate * 60 * channels
        chunk_num = 1

        for start_idx in range(0, len(samples), samples_per_minute):
            chunk_samples = samples[start_idx:start_idx + samples_per_minute]

            # Write WAV file
            wav_path = conv_dir / f"chunk_{chunk_num:03d}.wav"
            self._write_wav(wav_path, sample_rate, channels, chunk_samples)
            chunk_num += 1

    def _write_wav(self, path: Path, sample_rate: int, channels: int, samples: Tuple[int, ...]):
        """Write PCM samples to WAV file"""
        import wave

        with wave.open(str(path), 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)

            # Convert samples back to bytes
            pcm_bytes = struct.pack(f'<{len(samples)}h', *samples)
            wav_file.writeframes(pcm_bytes)

    async def _export_memories(self, qdrant_client: AsyncQdrantClient, stats: CleanupStats):
        """Export Qdrant vectors to JSON"""
        logger.info("Exporting memories from Qdrant...")

        try:
            collection_name = get_qdrant_collection_name()

            # Check if collection exists
            collections = await qdrant_client.get_collections()
            collection_exists = any(
                col.name == collection_name
                for col in collections.collections
            )

            if not collection_exists:
                logger.info("Memories collection does not exist, skipping export")
                return

            # Scroll through all vectors
            memories_data = []
            offset = None

            while True:
                result = await qdrant_client.scroll(
                    collection_name=collection_name,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True
                )

                points, next_offset = result

                if not points:
                    break

                for point in points:
                    memory_dict = {
                        'id': str(point.id),
                        'vector': point.vector,
                        'payload': point.payload
                    }
                    memories_data.append(memory_dict)

                if next_offset is None:
                    break

                offset = next_offset

            stats.memories_count = len(memories_data)

            output_path = self.backup_path / "memories.json"
            with open(output_path, 'w') as f:
                json.dump(memories_data, f, indent=2)

            logger.info(f"Exported {stats.memories_count} memories")

        except Exception as e:
            logger.warning(f"Failed to export memories: {e}")

    async def _generate_summary(self, stats: CleanupStats):
        """Generate backup summary"""
        summary = {
            'timestamp': self.timestamp,
            'backup_path': str(self.backup_path),
            'total_conversations': stats.conversations_count,
            'total_audio_chunks': stats.audio_chunks_count,
            'total_waveforms': stats.waveforms_count,
            'total_chat_sessions': stats.chat_sessions_count,
            'total_chat_messages': stats.chat_messages_count,
            'total_memories': stats.memories_count,
            'audio_exported': self.export_audio,
            'backup_size_bytes': 0  # Will be calculated after all files written
        }

        output_path = self.backup_path / "backup_summary.json"
        with open(output_path, 'w') as f:
            json.dump(summary, f, indent=2)


class CleanupManager:
    """Handle cleanup operations"""

    def __init__(
        self,
        mongo_db: Any,
        redis_conn: Any,
        qdrant_client: Optional[AsyncQdrantClient],
        include_wav: bool,
        delete_users: bool
    ):
        self.mongo_db = mongo_db
        self.redis_conn = redis_conn
        self.qdrant_client = qdrant_client
        self.include_wav = include_wav
        self.delete_users = delete_users

    async def perform_cleanup(self, stats: CleanupStats) -> bool:
        """Perform all cleanup operations"""
        try:
            logger.info("Starting cleanup operations...")

            # MongoDB cleanup
            await self._cleanup_mongodb(stats)

            # Qdrant cleanup
            if self.qdrant_client:
                await self._cleanup_qdrant(stats)

            # Redis cleanup
            self._cleanup_redis(stats)

            # Legacy WAV cleanup
            if self.include_wav:
                self._cleanup_legacy_wav(stats)

            logger.info("Cleanup completed successfully")
            return True

        except Exception as e:
            logger.error(f"Cleanup failed: {e}", exc_info=True)
            return False

    async def _cleanup_mongodb(self, stats: CleanupStats):
        """Clean MongoDB collections"""
        logger.info("Cleaning MongoDB collections...")

        # Count before deletion
        stats.conversations_count = await Conversation.find_all().count()
        # Use raw MongoDB count to handle malformed documents
        stats.audio_chunks_count = await self.mongo_db["audio_chunks"].count_documents({})
        stats.waveforms_count = await WaveformData.find_all().count()
        stats.chat_sessions_count = await self.mongo_db["chat_sessions"].count_documents({})
        stats.chat_messages_count = await self.mongo_db["chat_messages"].count_documents({})

        if self.delete_users:
            stats.users_count = await User.find_all().count()

        # Delete conversations
        result = await Conversation.find_all().delete()
        logger.info(f"Deleted {stats.conversations_count} conversations")

        # Delete audio chunks using raw MongoDB to handle malformed documents
        result = await self.mongo_db["audio_chunks"].delete_many({})
        logger.info(f"Deleted {stats.audio_chunks_count} audio chunks")

        # Delete waveforms
        result = await WaveformData.find_all().delete()
        logger.info(f"Deleted {stats.waveforms_count} waveforms")

        # Delete chat sessions
        result = await self.mongo_db["chat_sessions"].delete_many({})
        logger.info(f"Deleted {stats.chat_sessions_count} chat sessions")

        # Delete chat messages
        result = await self.mongo_db["chat_messages"].delete_many({})
        logger.info(f"Deleted {stats.chat_messages_count} chat messages")

        # Delete users if requested
        if self.delete_users:
            result = await User.find_all().delete()
            logger.info(f"DANGEROUS: Deleted {stats.users_count} users")

    async def _cleanup_qdrant(self, stats: CleanupStats):
        """Clean Qdrant vector store"""
        logger.info("Cleaning Qdrant memories...")

        try:
            collection_name = get_qdrant_collection_name()

            # Check if collection exists
            collections = await self.qdrant_client.get_collections()
            collection_exists = any(
                col.name == collection_name
                for col in collections.collections
            )

            if not collection_exists:
                logger.info("Memories collection does not exist, skipping cleanup")
                return

            # Get count before deletion
            collection_info = await self.qdrant_client.get_collection(collection_name)
            stats.memories_count = collection_info.points_count

            # Delete and recreate collection
            await self.qdrant_client.delete_collection(collection_name)
            logger.info(f"Deleted memories collection ({stats.memories_count} vectors)")

            # Recreate with default configuration
            await self.qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE)
            )
            logger.info("Recreated memories collection")

        except Exception as e:
            logger.warning(f"Failed to clean Qdrant: {e}")

    def _cleanup_redis(self, stats: CleanupStats):
        """Clean Redis job queues"""
        logger.info("Cleaning Redis job queues...")

        queue_names = ["transcription", "memory", "audio", "default"]
        successful_jobs = 0
        failed_jobs = 0
        failed_queues = []

        for queue_name in queue_names:
            job_count = 0  # Initialize to 0 in case counting fails
            try:
                queue = Queue(queue_name, connection=self.redis_conn)

                # Count jobs
                job_count = (
                    len(queue) +
                    len(queue.started_job_registry) +
                    len(queue.finished_job_registry) +
                    len(queue.failed_job_registry) +
                    len(queue.canceled_job_registry) +
                    len(queue.deferred_job_registry) +
                    len(queue.scheduled_job_registry)
                )

                # Clear queue and registries
                queue.empty()

                # Clear job registries (they don't have clear() method in all RQ versions)
                # So we manually remove all job IDs
                for job_id in queue.started_job_registry.get_job_ids():
                    queue.started_job_registry.remove(job_id)
                for job_id in queue.finished_job_registry.get_job_ids():
                    queue.finished_job_registry.remove(job_id)
                for job_id in queue.failed_job_registry.get_job_ids():
                    queue.failed_job_registry.remove(job_id)
                for job_id in queue.canceled_job_registry.get_job_ids():
                    queue.canceled_job_registry.remove(job_id)
                for job_id in queue.deferred_job_registry.get_job_ids():
                    queue.deferred_job_registry.remove(job_id)
                for job_id in queue.scheduled_job_registry.get_job_ids():
                    queue.scheduled_job_registry.remove(job_id)

                # Only count as successful if cleanup completed without exception
                successful_jobs += job_count
                logger.info(f"Cleared {queue_name} queue ({job_count} jobs)")

            except Exception as e:
                logger.error(f"Failed to clean {queue_name} queue: {e}", exc_info=True)
                # job_count might be 0 if counting failed, or partial count if cleanup failed
                failed_jobs += job_count
                failed_queues.append(queue_name)
                # Continue processing remaining queues

        stats.redis_jobs_count = successful_jobs
        if failed_queues:
            logger.warning(
                f"Cleared {successful_jobs} Redis jobs, failed to clear {failed_jobs} jobs from queues: {', '.join(failed_queues)}"
            )
        else:
            logger.info(f"Cleared total of {successful_jobs} Redis jobs")

    def _cleanup_legacy_wav(self, stats: CleanupStats):
        """Clean legacy WAV files"""
        logger.info("Cleaning legacy WAV files...")

        try:
            wav_dir = Path("/app/data/audio_chunks")

            if not wav_dir.exists():
                logger.info("Legacy WAV directory does not exist, skipping")
                return

            wav_files = list(wav_dir.glob("*.wav"))
            stats.legacy_wav_count = len(wav_files)

            for wav_file in wav_files:
                wav_file.unlink()

            logger.info(f"Deleted {stats.legacy_wav_count} legacy WAV files")

        except Exception as e:
            logger.warning(f"Failed to clean legacy WAV files: {e}")


async def get_current_stats(
    mongo_db: Any,
    redis_conn: Any,
    qdrant_client: Optional[AsyncQdrantClient]
) -> CleanupStats:
    """Get current statistics before cleanup"""
    stats = CleanupStats()

    # MongoDB counts
    stats.conversations_count = await Conversation.find_all().count()
    # Use raw MongoDB count to handle malformed documents
    stats.audio_chunks_count = await mongo_db["audio_chunks"].count_documents({})
    stats.waveforms_count = await WaveformData.find_all().count()
    stats.chat_sessions_count = await mongo_db["chat_sessions"].count_documents({})
    stats.chat_messages_count = await mongo_db["chat_messages"].count_documents({})
    stats.users_count = await User.find_all().count()

    # Qdrant count
    if qdrant_client:
        try:
            collection_name = get_qdrant_collection_name()
            collection_info = await qdrant_client.get_collection(collection_name)
            stats.memories_count = collection_info.points_count
        except Exception:
            stats.memories_count = 0

    # Redis count
    try:
        queue_names = ["transcription", "memory", "audio", "default"]
        total_jobs = 0
        for queue_name in queue_names:
            queue = Queue(queue_name, connection=redis_conn)
            total_jobs += (
                len(queue) +
                len(queue.started_job_registry) +
                len(queue.finished_job_registry) +
                len(queue.failed_job_registry) +
                len(queue.canceled_job_registry) +
                len(queue.deferred_job_registry) +
                len(queue.scheduled_job_registry)
            )
        stats.redis_jobs_count = total_jobs
    except Exception:
        stats.redis_jobs_count = 0

    # Legacy WAV count
    wav_dir = Path("/app/data/audio_chunks")
    if wav_dir.exists():
        stats.legacy_wav_count = len(list(wav_dir.glob("*.wav")))

    return stats


def print_stats(stats: CleanupStats, title: str = "Current State"):
    """Print statistics in a formatted way"""
    print(f"\n{'='*60}")
    print(f"{title:^60}")
    print(f"{'='*60}")
    print(f"Conversations:        {stats.conversations_count:>10}")
    print(f"Audio Chunks:         {stats.audio_chunks_count:>10}")
    print(f"Waveforms:            {stats.waveforms_count:>10}")
    print(f"Chat Sessions:        {stats.chat_sessions_count:>10}")
    print(f"Chat Messages:        {stats.chat_messages_count:>10}")
    print(f"Memories (Qdrant):    {stats.memories_count:>10}")
    print(f"Redis Jobs:           {stats.redis_jobs_count:>10}")
    print(f"Legacy WAV Files:     {stats.legacy_wav_count:>10}")
    print(f"Users:                {stats.users_count:>10}")
    if stats.backup_path:
        print(f"\nBackup Location:      {stats.backup_path}")
        if stats.backup_size_bytes > 0:
            size_mb = stats.backup_size_bytes / (1024**2)
            print(f"Backup Size:          {size_mb:>10.2f} MB")
    print(f"{'='*60}\n")


def confirm_action(message: str) -> bool:
    """Ask for user confirmation"""
    response = input(f"{message} (yes/no): ").strip().lower()
    return response == 'yes'


async def main():
    parser = argparse.ArgumentParser(
        description='Clean Chronicle backend state with optional backup',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run to see what would be deleted
  python cleanup_state.py --dry-run

  # Safe cleanup with metadata backup
  python cleanup_state.py --backup

  # Full backup including audio export
  python cleanup_state.py --backup --export-audio

  # Automated cleanup without confirmation
  python cleanup_state.py --backup --force
        """
    )

    parser.add_argument(
        '--backup',
        action='store_true',
        help='Create backup before cleaning (metadata only by default)'
    )
    parser.add_argument(
        '--export-audio',
        action='store_true',
        help='Include audio WAV export in backup (can be large, requires --backup)'
    )
    parser.add_argument(
        '--include-wav',
        action='store_true',
        help='Include legacy WAV file cleanup (backward compat)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be cleaned without deleting'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt'
    )
    parser.add_argument(
        '--backup-dir',
        type=str,
        default='/app/data/backups',
        help='Backup directory location (default: /app/data/backups)'
    )
    parser.add_argument(
        '--delete-users',
        action='store_true',
        help='DANGEROUS: Also delete user accounts'
    )

    args = parser.parse_args()

    # Validate arguments
    if args.export_audio and not args.backup:
        logger.error("--export-audio requires --backup")
        sys.exit(1)

    # Initialize connections
    logger.info("Connecting to services...")

    # MongoDB
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
    mongodb_database = os.getenv("MONGODB_DATABASE", "chronicle")
    mongo_client = AsyncIOMotorClient(mongodb_uri)
    mongo_db = mongo_client[mongodb_database]

    # Initialize Beanie
    await init_beanie(
        database=mongo_db,
        document_models=[Conversation, AudioChunkDocument, WaveformData, User]
    )

    # Redis
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis_conn = redis.from_url(redis_url)

    # Qdrant
    qdrant_client = None
    try:
        qdrant_host = os.getenv("QDRANT_BASE_URL", "qdrant")
        qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
        qdrant_client = AsyncQdrantClient(host=qdrant_host, port=qdrant_port)
    except Exception as e:
        logger.warning(f"Qdrant not available: {e}")

    # Get current statistics
    logger.info("Gathering current statistics...")
    stats = await get_current_stats(mongo_db, redis_conn, qdrant_client)

    # Print current state
    print_stats(stats, "Current Backend State")

    # Dry-run mode
    if args.dry_run:
        print("\n[DRY-RUN MODE] No actual changes will be made\n")
        if args.backup:
            print("Would create backup at:", Path(args.backup_dir) / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            if args.export_audio:
                print("Would include audio WAV export (1-minute chunks)")
        print("\nWould delete:")
        print(f"  - {stats.conversations_count} conversations")
        print(f"  - {stats.audio_chunks_count} audio chunks")
        print(f"  - {stats.waveforms_count} waveforms")
        print(f"  - {stats.chat_sessions_count} chat sessions")
        print(f"  - {stats.chat_messages_count} chat messages")
        print(f"  - {stats.memories_count} memories")
        print(f"  - {stats.redis_jobs_count} Redis jobs")
        if args.include_wav:
            print(f"  - {stats.legacy_wav_count} legacy WAV files")
        if args.delete_users:
            print(f"  - {stats.users_count} users (DANGEROUS)")
        else:
            print(f"  - Users will be preserved ({stats.users_count} users)")
        print("\nRun without --dry-run to perform actual cleanup")
        return

    # Confirmation prompt
    if not args.force:
        print("\n⚠️  WARNING: This will permanently delete data!")
        print(f"  - {stats.conversations_count} conversations")
        print(f"  - {stats.audio_chunks_count} audio chunks")
        print(f"  - {stats.waveforms_count} waveforms")
        print(f"  - {stats.chat_sessions_count} chat sessions")
        print(f"  - {stats.chat_messages_count} chat messages")
        print(f"  - {stats.memories_count} memories")
        print(f"  - {stats.redis_jobs_count} Redis jobs")
        if args.include_wav:
            print(f"  - {stats.legacy_wav_count} legacy WAV files")
        if args.delete_users:
            print(f"  - {stats.users_count} users (DANGEROUS)")
        else:
            print(f"  - Users will be preserved ({stats.users_count} users)")

        if args.backup:
            print(f"\n✓ Backup will be created at: {args.backup_dir}")
            if args.export_audio:
                print("✓ Audio will be exported as WAV files")
        else:
            print("\n✗ No backup will be created")

        print()
        if not confirm_action("Are you sure you want to proceed?"):
            logger.info("Cleanup cancelled by user")
            return

    # Create backup if requested
    if args.backup:
        backup_manager = BackupManager(args.backup_dir, args.export_audio, mongo_db)
        success = await backup_manager.create_backup(qdrant_client, stats)

        if not success:
            logger.error("Backup failed, aborting cleanup")
            return

        print_stats(stats, "Backup Created")

    # Perform cleanup
    cleanup_manager = CleanupManager(
        mongo_db,
        redis_conn,
        qdrant_client,
        args.include_wav,
        args.delete_users
    )

    success = await cleanup_manager.perform_cleanup(stats)

    if not success:
        logger.error("Cleanup failed")
        return

    # Verify cleanup
    logger.info("Verifying cleanup...")
    final_stats = await get_current_stats(mongo_db, redis_conn, qdrant_client)
    print_stats(final_stats, "Backend State After Cleanup")

    logger.info("✓ Cleanup completed successfully!")

    if args.backup:
        logger.info(f"✓ Backup saved to: {stats.backup_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nCleanup interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
