"""
Audio file upload and processing controller.

Handles audio file uploads and processes them directly.
Simplified to write files immediately and enqueue transcription.

Also includes audio cropping operations that work with the Conversation model.
"""

import logging
import os
import time
import uuid

from fastapi import UploadFile
from fastapi.responses import JSONResponse

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    start_post_conversation_jobs,
    transcription_queue,
)
from advanced_omi_backend.models.conversation import create_conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.transcription import is_transcription_available
from advanced_omi_backend.utils.audio_chunk_utils import convert_audio_to_chunks
from advanced_omi_backend.utils.audio_utils import (
    SUPPORTED_AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AudioValidationError,
    convert_any_to_wav,
    validate_and_prepare_audio,
)
from advanced_omi_backend.workers.transcription_jobs import (
    transcribe_full_audio_job,
)

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


def generate_client_id(user: User, device_name: str) -> str:
    """Generate client ID for uploaded files."""
    logger.debug(f"Generating client ID - user.id={user.id}, type={type(user.id)}")
    user_id_suffix = str(user.id)[-6:]
    return f"{user_id_suffix}-{device_name}"


async def upload_and_process_audio_files(
    user: User,
    files: list[UploadFile],
    device_name: str = "upload",
    source: str = "upload"
) -> dict:
    """
    Upload audio files and process them directly.

    Simplified flow:
    1. Validate and read WAV file
    2. Write audio file and create AudioSession immediately
    3. Enqueue transcription job (same as WebSocket path)

    Args:
        user: Authenticated user
        files: List of uploaded audio files
        device_name: Device identifier
        source: Source of the upload (e.g., 'upload', 'gdrive')
    """
    try:
        if not files:
            return JSONResponse(status_code=400, content={"error": "No files provided"})

        processed_files = []
        client_id = generate_client_id(user, device_name)

        for file_index, file in enumerate(files):
            try:
                # Validate file type
                filename = file.filename or "unknown"
                _, ext = os.path.splitext(filename.lower())
                if not ext or ext not in SUPPORTED_AUDIO_EXTENSIONS:
                    supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
                    processed_files.append({
                        "filename": filename,
                        "status": "error",
                        "error": f"Unsupported format '{ext}'. Supported: {supported}",
                    })
                    continue

                is_video_source = ext in VIDEO_EXTENSIONS

                audio_logger.info(
                    f"📁 Uploading file {file_index + 1}/{len(files)}: {filename}"
                )

                # Read file content
                content = await file.read()

                # Convert non-WAV files to WAV via FFmpeg
                if ext != ".wav":
                    try:
                        content = await convert_any_to_wav(content, ext)
                    except AudioValidationError as e:
                        processed_files.append({
                            "filename": filename,
                            "status": "error",
                            "error": str(e),
                        })
                        continue

                # Track external source for deduplication (Google Drive, etc.)
                external_source_id = None
                external_source_type = None
                if source == "gdrive":
                    external_source_id = getattr(file, "file_id", None) or getattr(file, "audio_uuid", None)
                    external_source_type = "gdrive"
                    if not external_source_id:
                        audio_logger.warning(f"Missing file_id for gdrive file: {filename}")
                timestamp = int(time.time() * 1000)

                # Validate and prepare audio (read format from WAV file)
                try:
                    audio_data, sample_rate, sample_width, channels, duration = await validate_and_prepare_audio(
                        audio_data=content,
                        expected_sample_rate=16000,  # Expecting 16kHz
                        convert_to_mono=True,  # Convert stereo to mono
                        auto_resample=True  # Auto-resample if sample rate doesn't match
                    )
                except AudioValidationError as e:
                    processed_files.append({
                        "filename": filename,
                        "status": "error",
                        "error": str(e),
                    })
                    continue

                audio_logger.info(
                    f"📊 {filename}: {duration:.1f}s ({sample_rate}Hz, {channels}ch, {sample_width} bytes/sample)"
                )

                # Generate title from filename
                title = filename.rsplit('.', 1)[0][:50] if filename != "unknown" else "Uploaded Audio"

                conversation = create_conversation(
                    user_id=user.user_id,
                    client_id=client_id,
                    title=title,
                    summary="Processing uploaded audio file...",
                    external_source_id=external_source_id,
                    external_source_type=external_source_type,
                )
                await conversation.insert()
                conversation_id = conversation.conversation_id  # Get the auto-generated ID

                audio_logger.info(f"📝 Created conversation {conversation_id} for uploaded file")

                # Convert audio directly to MongoDB chunks
                try:
                    num_chunks = await convert_audio_to_chunks(
                        conversation_id=conversation_id,
                        audio_data=audio_data,
                        sample_rate=sample_rate,
                        channels=channels,
                        sample_width=sample_width,
                    )
                    audio_logger.info(
                        f"📦 Converted uploaded file to {num_chunks} MongoDB chunks "
                        f"(conversation {conversation_id[:12]})"
                    )
                except ValueError as val_error:
                    # Handle validation errors (e.g., file too long)
                    audio_logger.error(f"Audio validation failed: {val_error}")
                    processed_files.append({
                        "filename": filename,
                        "status": "error",
                        "error": str(val_error),
                    })
                    # Delete the conversation since it won't have audio chunks
                    await conversation.delete()
                    continue
                except Exception as chunk_error:
                    audio_logger.error(
                        f"Failed to convert uploaded file to chunks: {chunk_error}",
                        exc_info=True
                    )
                    processed_files.append({
                        "filename": filename,
                        "status": "error",
                        "error": f"Audio conversion failed: {str(chunk_error)}",
                    })
                    # Delete the conversation since it won't have audio chunks
                    await conversation.delete()
                    continue

                # Enqueue batch transcription job first (file uploads need transcription)
                version_id = str(uuid.uuid4())
                transcribe_job_id = f"transcribe_{conversation_id[:12]}"

                # Check if transcription provider is available before enqueueing
                transcription_job = None
                if is_transcription_available(mode="batch"):
                    transcription_job = transcription_queue.enqueue(
                        transcribe_full_audio_job,
                        conversation_id,
                        version_id,
                        "batch",  # trigger
                        job_timeout=900,  # 15 minutes
                        result_ttl=JOB_RESULT_TTL,
                        job_id=transcribe_job_id,
                        description=f"Transcribe uploaded file {conversation_id[:8]}",
                        meta={'conversation_id': conversation_id, 'client_id': client_id}
                    )
                    audio_logger.info(f"📥 Enqueued transcription job {transcription_job.id} for uploaded file")
                else:
                    audio_logger.warning(
                        f"⚠️ Skipping transcription for conversation {conversation_id}: "
                        "No transcription provider configured"
                    )

                # Enqueue post-conversation processing job chain (depends on transcription)
                job_ids = start_post_conversation_jobs(
                    conversation_id=conversation_id,
                    user_id=user.user_id,
                    transcript_version_id=version_id,  # Pass the version_id from transcription job
                    depends_on_job=transcription_job,  # Wait for transcription to complete (or None)
                    client_id=client_id  # Pass client_id for UI tracking
                )

                file_result = {
                    "filename": filename,
                    "status": "started",  # RQ standard: job has been enqueued
                    "conversation_id": conversation_id,
                    "transcript_job_id": transcription_job.id if transcription_job else None,
                    "speaker_job_id": job_ids['speaker_recognition'],
                    "memory_job_id": job_ids['memory'],
                    "duration_seconds": round(duration, 2),
                }
                if is_video_source:
                    file_result["note"] = "Audio extracted from video file"
                processed_files.append(file_result)

                # Build job chain description
                job_chain = []
                if transcription_job:
                    job_chain.append(transcription_job.id)
                if job_ids['speaker_recognition']:
                    job_chain.append(job_ids['speaker_recognition'])
                if job_ids['memory']:
                    job_chain.append(job_ids['memory'])

                audio_logger.info(
                    f"✅ Processed {filename} → conversation {conversation_id}, "
                    f"jobs: {' → '.join(job_chain) if job_chain else 'none'}"
                )

            except (OSError, IOError) as e:
                # File I/O errors during audio processing
                audio_logger.exception(f"File I/O error processing {filename}")
                processed_files.append({
                    "filename": filename,
                    "status": "error",
                    "error": str(e),
                })
            except Exception as e:
                # Unexpected errors during file processing
                audio_logger.exception(f"Unexpected error processing file {filename}")
                processed_files.append({
                    "filename": filename,
                    "status": "error",
                    "error": str(e),
                })

        successful_files = [f for f in processed_files if f.get("status") == "started"]
        failed_files = [f for f in processed_files if f.get("status") == "error"]

        response_body = {
            "message": f"Uploaded and processing {len(successful_files)} file(s)",
            "client_id": client_id,
            "files": processed_files,
            "summary": {
                "total": len(files),
                "started": len(successful_files),  # RQ standard
                "failed": len(failed_files),
            },
        }

        # Return appropriate HTTP status code based on results
        if len(failed_files) == len(files):
            # ALL files failed - return 400 Bad Request
            audio_logger.error(f"All {len(files)} file(s) failed to upload")
            return JSONResponse(status_code=400, content=response_body)
        elif len(failed_files) > 0:
            # SOME files failed (partial success) - return 207 Multi-Status
            audio_logger.warning(f"Partial upload: {len(successful_files)} succeeded, {len(failed_files)} failed")
            return JSONResponse(status_code=207, content=response_body)
        else:
            # All files succeeded - return 200 OK
            return response_body

    except (OSError, IOError) as e:
        # File system errors during upload handling
        audio_logger.exception("File I/O error in upload_and_process_audio_files")
        return JSONResponse(
            status_code=500, content={"error": f"File upload failed: {str(e)}"}
        )
    except Exception as e:
        # Unexpected errors in upload handler
        audio_logger.exception("Unexpected error in upload_and_process_audio_files")
        return JSONResponse(
            status_code=500, content={"error": f"File upload failed: {str(e)}"}
        )
