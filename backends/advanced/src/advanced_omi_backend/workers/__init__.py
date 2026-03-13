"""
Workers package - RQ job definitions and queue utilities.

This package provides modular RQ job functions organized by domain:
- transcription_jobs: Speech-to-text processing
- speaker_jobs: Speaker recognition and identification
- conversation_jobs: Conversation management and updates
- memory_jobs: Memory extraction and processing
- audio_jobs: Audio file processing

Queue configuration and utilities are in controllers/queue_controller.py
"""

# Import from queue_controller
from advanced_omi_backend.controllers.queue_controller import (
    DEFAULT_QUEUE,
    JOB_RESULT_TTL,
    MEMORY_QUEUE,
    REDIS_URL,
    TRANSCRIPTION_QUEUE,
    default_queue,
    get_job_stats,
    get_jobs,
    get_queue,
    get_queue_health,
    memory_queue,
    redis_conn,
    transcription_queue,
)

# Import from job models
from advanced_omi_backend.models.job import _ensure_beanie_initialized

# Import from audio_jobs
from .audio_jobs import audio_streaming_persistence_job

# Import from conversation_jobs
from .conversation_jobs import open_conversation_job

# Import from memory_jobs
from .memory_jobs import enqueue_memory_processing, process_memory_job

# Import from speaker_jobs
from .speaker_jobs import check_enrolled_speakers_job, recognise_speakers_job

# Import from transcription_jobs
from .transcription_jobs import stream_speech_detection_job, transcribe_full_audio_job

__all__ = [
    # Transcription jobs
    "transcribe_full_audio_job",
    "stream_speech_detection_job",
    # Speaker jobs
    "check_enrolled_speakers_job",
    "recognise_speakers_job",
    # Conversation jobs
    "open_conversation_job",
    "audio_streaming_persistence_job",
    # Memory jobs
    "process_memory_job",
    "enqueue_memory_processing",
    # Queue utils
    "get_queue",
    "get_job_stats",
    "get_jobs",
    "get_queue_health",
    "transcription_queue",
    "memory_queue",
    "default_queue",
    "redis_conn",
    "REDIS_URL",
    "JOB_RESULT_TTL",
    "_ensure_beanie_initialized",
    "TRANSCRIPTION_QUEUE",
    "MEMORY_QUEUE",
    "DEFAULT_QUEUE",
]
