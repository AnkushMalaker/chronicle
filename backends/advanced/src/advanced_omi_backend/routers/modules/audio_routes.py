"""
Audio file upload and serving routes.

Handles audio file uploads, processing job management, and audio file serving.
Audio is served from MongoDB chunks with Opus compression.
"""

import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from advanced_omi_backend.app_config import get_audio_chunk_dir
from advanced_omi_backend.auth import (
    current_active_user_optional,
    current_superuser,
    get_user_from_token_param,
)
from advanced_omi_backend.controllers import audio_controller
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.utils.audio_chunk_utils import (
    build_wav_from_pcm,
    concatenate_chunks_to_pcm,
    reconstruct_wav_from_conversation,
    retrieve_audio_chunks,
)
from advanced_omi_backend.utils.gdrive_audio_utils import (
    AudioValidationError,
    download_audio_files_from_drive,
)

router = APIRouter(prefix="/audio", tags=["audio"])


def _safe_filename(conversation: "Conversation") -> str:
    """Build a filesystem-safe filename from the conversation title, falling back to ID."""
    title = conversation.title
    if not title:
        return conversation.conversation_id
    # Replace anything that isn't alphanumeric, space, hyphen, or underscore
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    # Collapse whitespace to single underscore
    safe = re.sub(r"\s+", "_", safe)
    return safe[:120] or conversation.conversation_id


@router.post("/upload_audio_from_gdrive")
async def upload_audio_from_drive_folder(
    gdrive_folder_id: str = Query(..., description="Google Drive Folder ID containing audio files (e.g., the string after /folders/ in the URL)"),
    current_user: User = Depends(current_superuser),
    device_name: str = Query(default="upload"),
):
    try:
        files = await download_audio_files_from_drive(gdrive_folder_id, current_user.id)
    except AudioValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return await audio_controller.upload_and_process_audio_files(
        current_user, files, device_name, source="gdrive"
    )


@router.get("/get_audio/{conversation_id}")
async def get_conversation_audio(
    conversation_id: str,
    request: Request,
    token: Optional[str] = Query(default=None, description="JWT token for audio element access"),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Serve complete audio file for a conversation from MongoDB chunks.

    Reconstructs audio by:
    1. Retrieving all Opus-compressed chunks from MongoDB
    2. Decoding each chunk to PCM
    3. Concatenating PCM data
    4. Building complete WAV file with headers

    Supports both header-based auth (Authorization: Bearer) and query param token
    for <audio> element compatibility.

    Args:
        conversation_id: The conversation ID
        token: Optional JWT token as query param (for audio elements)
        current_user: Authenticated user (from header)

    Returns:
        StreamingResponse with complete WAV file

    Raises:
        404: If conversation or audio chunks not found
        403: If user doesn't own the conversation
        401: If not authenticated
    """
    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Reconstruct WAV from MongoDB chunks
    try:
        wav_data = await reconstruct_wav_from_conversation(conversation_id)
    except ValueError as e:
        # No chunks found for conversation
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Reconstruction failed
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reconstruct audio: {str(e)}"
        )

    # Handle Range requests for seeking support
    file_size = len(wav_data)
    range_header = request.headers.get("range")
    filename = _safe_filename(conversation)

    # If no Range header, return complete file
    if not range_header:
        return StreamingResponse(
            io.BytesIO(wav_data),
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="{filename}.wav"',
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "X-Audio-Source": "mongodb-chunks",
                "X-Chunk-Count": str(conversation.audio_chunks_count or 0),
            }
        )

    # Parse Range header (e.g., "bytes=0-1023")
    try:
        range_str = range_header.replace("bytes=", "")
        range_start, range_end = range_str.split("-")
        range_start = int(range_start) if range_start else 0
        range_end = int(range_end) if range_end else file_size - 1

        # Ensure valid range
        range_start = max(0, range_start)
        range_end = min(file_size - 1, range_end)
        content_length = range_end - range_start + 1

        # Extract requested byte range
        range_data = wav_data[range_start:range_end + 1]

        # Return 206 Partial Content with Range headers
        return Response(
            content=range_data,
            status_code=206,
            media_type="audio/wav",
            headers={
                "Content-Range": f"bytes {range_start}-{range_end}/{file_size}",
                "Content-Length": str(content_length),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{filename}.wav"',
                "X-Audio-Source": "mongodb-chunks",
            }
        )
    except (ValueError, IndexError) as e:
        # Invalid Range header, return 416 Range Not Satisfiable
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{file_size}"
            }
        )


@router.get("/stream_audio/{conversation_id}")
async def stream_conversation_audio(
    conversation_id: str,
    token: Optional[str] = Query(default=None, description="JWT token for audio element access"),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Stream audio file for a conversation with progressive chunk delivery.

    Better UX for long conversations - starts playback before full download completes.

    Uses cursor-based pagination to stream chunks in batches of 20, decoding
    and serving each batch as it's retrieved.

    Supports both header-based auth (Authorization: Bearer) and query param token
    for <audio> element compatibility.

    Args:
        conversation_id: The conversation ID
        token: Optional JWT token as query param (for audio elements)
        current_user: Authenticated user (from header)

    Returns:
        StreamingResponse with chunked WAV data (Transfer-Encoding: chunked)

    Raises:
        404: If conversation or audio chunks not found
        403: If user doesn't own the conversation
        401: If not authenticated
    """
    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if chunks exist
    if not conversation.audio_chunks_count or conversation.audio_chunks_count == 0:
        raise HTTPException(status_code=404, detail="No audio data for this conversation")

    async def stream_chunks():
        """Generator that yields WAV data in batches."""
        # First, yield WAV header with placeholder size
        # (actual size will be updated by client or ignored in streaming mode)
        SAMPLE_RATE = 16000
        CHANNELS = 1
        SAMPLE_WIDTH = 2

        # Build minimal WAV header (44 bytes)
        # We'll write a placeholder size since we're streaming
        wav_header = io.BytesIO()
        import wave
        with wave.open(wav_header, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            # Write empty frame to establish header
            wav.writeframes(b"")

        # Yield header
        yield wav_header.getvalue()

        # Stream chunks in batches of 20
        start_index = 0
        batch_size = 20

        while start_index < conversation.audio_chunks_count:
            # Retrieve batch of chunks
            chunks = await retrieve_audio_chunks(
                conversation_id=conversation_id,
                start_index=start_index,
                limit=batch_size
            )

            if not chunks:
                break

            # Decode and concatenate this batch
            pcm_batch = await concatenate_chunks_to_pcm(chunks)

            # Yield PCM data (client's WAV parser handles the stream)
            yield pcm_batch

            # Move to next batch
            start_index += batch_size

    filename = _safe_filename(conversation)
    return StreamingResponse(
        stream_chunks(),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'inline; filename="{filename}.wav"',
            "X-Audio-Source": "mongodb-chunks-stream",
            "X-Chunk-Count": str(conversation.audio_chunks_count or 0),
            "X-Total-Duration": str(conversation.audio_total_duration or 0),
        }
    )


@router.get("/chunks/{conversation_id}")
async def get_audio_chunk_range(
    conversation_id: str,
    start_time: float = Query(..., description="Start time in seconds"),
    end_time: float = Query(..., description="End time in seconds"),
    token: Optional[str] = Query(default=None, description="JWT token for audio element access"),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Serve specific audio chunks by time range for seekable audio player.

    Returns PCM audio data for the requested time range without decoding
    the entire conversation. Enables efficient seeking in the UI player.

    Example:
        GET /api/audio/chunks/uuid?start_time=15.5&end_time=25.5&token=xxx
        Returns: 10 seconds of audio from 15.5s to 25.5s

    Args:
        conversation_id: The conversation ID
        start_time: Start time in seconds (inclusive)
        end_time: End time in seconds (inclusive)
        token: Optional JWT token as query param
        current_user: Authenticated user (from header)

    Returns:
        StreamingResponse with WAV file for requested range

    Raises:
        404: If conversation or audio chunks not found
        403: If user doesn't own the conversation
        401: If not authenticated
        400: If time range is invalid
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"🎵 Audio chunk request: conversation={conversation_id[:8]}..., start={start_time:.2f}s, end={end_time:.2f}s")

    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(current_user.user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Validate time range
    if start_time < 0 or end_time <= start_time:
        raise HTTPException(status_code=400, detail="Invalid time range")

    if conversation.audio_total_duration and end_time > conversation.audio_total_duration:
        end_time = conversation.audio_total_duration

    # Use the dedicated segment reconstruction function
    from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

    try:
        wav_data = await reconstruct_audio_segment(conversation_id, start_time, end_time)
        logger.info(f"✅ Returning WAV: {len(wav_data)} bytes for range {start_time:.2f}s - {end_time:.2f}s")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to reconstruct audio segment: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to reconstruct audio: {str(e)}")

    return StreamingResponse(
        io.BytesIO(wav_data),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"inline; filename=chunk_{start_time}_{end_time}.wav",
            "Content-Length": str(len(wav_data)),
            "X-Audio-Duration": str(end_time - start_time),
            "X-Start-Time": str(start_time),
            "X-End-Time": str(end_time),
        }
    )


@router.post("/upload")
async def upload_audio_files(
    current_user: User = Depends(current_superuser),
    files: list[UploadFile] = File(...),
    device_name: str = Query(default="upload", description="Device name for uploaded files"),
):
    """
    Upload and process audio files. Admin only.

    Audio files are stored as MongoDB chunks and enqueued for processing via RQ jobs.
    This allows for scalable processing of large files without blocking the API.

    Returns:
        - List of uploaded files with their processing job IDs
        - Summary of enqueued vs failed uploads
    """
    return await audio_controller.upload_and_process_audio_files(
        current_user, files, device_name
    )
