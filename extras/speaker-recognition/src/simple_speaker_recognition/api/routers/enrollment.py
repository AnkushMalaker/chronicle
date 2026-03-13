"""Speaker enrollment endpoints."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from simple_speaker_recognition.api.core.utils import (
    extract_user_id_from_speaker_id,
    get_data_directory,
    secure_temp_file,
)
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker
from simple_speaker_recognition.utils.audio_processing import get_audio_info

# These will be imported from the main service.py when we integrate
# from ..service import get_db, audio_backend, auth

router = APIRouter()
log = logging.getLogger("speaker_service")


# Import dependencies from parent service module
async def get_db():
    """Get speaker database dependency."""
    from .. import service

    return await service.get_db()


def get_audio_backend():
    """Get audio backend."""
    from .. import service

    return service.audio_backend


def get_auth():
    """Get auth settings."""
    from .. import service

    return service.auth


def check_duplicate_speaker_name(
    user_id: int, speaker_name: str, exclude_speaker_id: str = None
) -> bool:
    """Check if a speaker name already exists for the given user.

    Args:
        user_id: User ID to check within
        speaker_name: Speaker name to check
        exclude_speaker_id: Speaker ID to exclude from check (for updates)

    Returns:
        True if duplicate found, False otherwise
    """
    db_session = get_db_session()
    try:
        query = db_session.query(Speaker).filter(
            Speaker.user_id == user_id, Speaker.name == speaker_name
        )

        if exclude_speaker_id:
            query = query.filter(Speaker.id != exclude_speaker_id)

        existing_speaker = query.first()
        return existing_speaker is not None

    finally:
        db_session.close()


def save_enrollment_audio(
    user_id: int,
    speaker_id: str,
    audio_data: bytes,
    filename: str,
    enrollment_type: str = "upload",
) -> Path:
    """Save enrollment audio file to disk.

    Args:
        user_id: User ID
        speaker_id: Speaker ID
        audio_data: Audio file data
        filename: Original filename
        enrollment_type: Type of enrollment (upload, recording, append)

    Returns:
        Path to saved audio file
    """
    # Create directory structure: data/enrollment_audio/{user_id}/{speaker_id}/
    auth = get_auth()
    speaker_audio_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
    speaker_audio_dir.mkdir(parents=True, exist_ok=True)

    # Generate unique filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = Path(filename).stem.replace(" ", "_").replace("/", "_")
    extension = Path(filename).suffix or ".wav"
    unique_filename = f"{timestamp}_{enrollment_type}_{safe_filename}{extension}"

    # Save audio file
    audio_path = speaker_audio_dir / unique_filename
    with open(audio_path, "wb") as f:
        f.write(audio_data)

    log.info(f"Saved enrollment audio: {audio_path}")
    return audio_path


def save_enrollment_manifest(
    user_id: int, speaker_id: str, audio_files: List[dict]
) -> Path:
    """Save or update enrollment manifest file.

    Args:
        user_id: User ID
        speaker_id: Speaker ID
        audio_files: List of audio file information

    Returns:
        Path to manifest file
    """
    auth = get_auth()
    manifest_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
    manifest_path = manifest_dir / "enrollment_manifest.json"

    # Load existing manifest if it exists
    existing_files = []
    if manifest_path.exists():
        try:
            with open(manifest_path, "r") as f:
                manifest_data = json.load(f)
                existing_files = manifest_data.get("audio_files", [])
        except Exception as e:
            log.warning(f"Failed to load existing manifest: {e}")

    # Combine existing and new files
    all_files = existing_files + audio_files

    # Create manifest data
    manifest_data = {
        "speaker_id": speaker_id,
        "user_id": user_id,
        "total_files": len(all_files),
        "last_updated": datetime.now().isoformat(),
        "audio_files": all_files,
    }

    # Save manifest
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)

    log.info(f"Updated enrollment manifest: {manifest_path}")
    return manifest_path


@router.post("/enroll/upload")
async def enroll_upload(
    file: UploadFile = File(..., description="WAV/FLAC <3 min"),
    speaker_id: str = Form(..., description="Unique speaker identifier"),
    speaker_name: str = Form(..., description="Speaker display name"),
    start: Optional[float] = Form(None, description="Start time in seconds"),
    end: Optional[float] = Form(None, description="End time in seconds"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Enroll a speaker from uploaded audio file."""
    # Extract user_id from speaker_id
    user_id = extract_user_id_from_speaker_id(speaker_id)
    log.info(f"Enrolling speaker: {speaker_name} (ID: {speaker_id}, User: {user_id})")

    # Check for duplicate speaker name (allow updates to existing speaker with same ID)
    if check_duplicate_speaker_name(
        user_id, speaker_name, exclude_speaker_id=speaker_id
    ):
        raise HTTPException(
            400,
            f"Speaker name '{speaker_name}' already exists for this user. Please choose a different name.",
        )

    # Read file content
    file_content = await file.read()

    # Persist temporary file for processing
    with secure_temp_file() as tmp:
        tmp.write(file_content)
        tmp_path = Path(tmp.name)
    try:
        log.info(f"Loading audio file: {tmp_path}")
        audio_backend = get_audio_backend()
        wav = audio_backend.load_wave(tmp_path, start, end)
        log.info(f"Audio loaded, shape: {wav.shape}")

        # Get audio info
        audio_info = get_audio_info(str(tmp_path))
        duration = audio_info["duration_seconds"]

        log.info("Computing speaker embedding...")
        emb = await audio_backend.async_embed(wav)
        log.info(f"Embedding computed, shape: {emb.shape}")

        # Save audio file to enrollment directory
        saved_path = save_enrollment_audio(
            user_id=user_id,
            speaker_id=speaker_id,
            audio_data=file_content,
            filename=file.filename or "upload.wav",
            enrollment_type="upload",
        )

        # Create manifest entry
        auth = get_auth()
        audio_file_info = {
            "filename": saved_path.name,
            "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
            "duration_seconds": duration,
            "start_time": start,
            "end_time": end,
            "upload_time": datetime.now().isoformat(),
            "original_filename": file.filename,
        }

        # Save manifest
        save_enrollment_manifest(user_id, speaker_id, [audio_file_info])

        log.info(f"Adding speaker to database...")
        updated = await db.add_speaker(
            speaker_id,
            speaker_name,
            emb[0],
            user_id,
            sample_count=1,
            total_duration=duration,
        )

        if updated:
            log.info(f"Successfully updated existing speaker: {speaker_id}")
        else:
            log.info(f"Successfully enrolled new speaker: {speaker_id}")

        return {
            "updated": updated,
            "speaker_id": speaker_id,
            "audio_saved": True,
            "audio_path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
        }
    except Exception as e:
        log.error(f"Error during enrollment: {e}")
        raise HTTPException(500, f"Enrollment failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/enroll/batch")
async def enroll_batch(
    files: List[UploadFile] = File(
        ..., description="Multiple audio files for same speaker"
    ),
    speaker_id: str = Form(..., description="Unique speaker identifier"),
    speaker_name: str = Form(..., description="Speaker display name"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Enroll a speaker using multiple audio segments, computing average embedding."""
    # Extract user_id from speaker_id
    user_id = extract_user_id_from_speaker_id(speaker_id)
    log.info(
        f"Batch enrolling speaker: {speaker_name} (ID: {speaker_id}, User: {user_id}) with {len(files)} files"
    )

    # Check for duplicate speaker name (allow updates to existing speaker with same ID)
    if check_duplicate_speaker_name(
        user_id, speaker_name, exclude_speaker_id=speaker_id
    ):
        raise HTTPException(
            400,
            f"Speaker name '{speaker_name}' already exists for this user. Please choose a different name.",
        )

    embeddings = []
    temp_paths = []
    total_duration = 0.0
    saved_audio_files = []

    try:
        # Process each audio file
        for i, file in enumerate(files):
            log.info(f"Processing file {i+1}/{len(files)}: {file.filename}")

            # Read file content
            file_content = await file.read()

            # Save to temporary file for processing
            with secure_temp_file() as tmp:
                tmp.write(file_content)
                tmp_path = Path(tmp.name)
                temp_paths.append(tmp_path)

            # Load and embed
            try:
                # Get accurate duration from original file
                audio_info = get_audio_info(str(tmp_path))
                duration = audio_info["duration_seconds"]
                total_duration += duration

                audio_backend = get_audio_backend()
                wav = audio_backend.load_wave(tmp_path)
                emb = await audio_backend.async_embed(wav)
                embeddings.append(emb[0])

                # Save audio file to enrollment directory
                saved_path = save_enrollment_audio(
                    user_id=user_id,
                    speaker_id=speaker_id,
                    audio_data=file_content,
                    filename=file.filename or f"batch_{i+1}.wav",
                    enrollment_type="batch",
                )

                # Track saved audio file info
                auth = get_auth()
                audio_file_info = {
                    "filename": saved_path.name,
                    "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
                    "duration_seconds": duration,
                    "upload_time": datetime.now().isoformat(),
                    "original_filename": file.filename,
                    "batch_index": i + 1,
                }
                saved_audio_files.append(audio_file_info)

                log.info(
                    f"Successfully embedded and saved file {i+1}, duration: {duration:.2f}s"
                )
            except Exception as e:
                log.warning(f"Failed to process file {i+1}: {e}")
                continue

        if not embeddings:
            raise HTTPException(400, "No valid audio files could be processed")

        # Save manifest with all audio files
        save_enrollment_manifest(user_id, speaker_id, saved_audio_files)

        # Compute average embedding
        log.info(f"Computing average embedding from {len(embeddings)} segments")
        embeddings_array = np.array(embeddings)
        average_embedding = np.mean(embeddings_array, axis=0)

        # Normalize the average embedding
        average_embedding = average_embedding / np.linalg.norm(average_embedding)

        log.info(f"Average embedding computed, shape: {average_embedding.shape}")

        # Add to database with proper counts
        updated = await db.add_speaker(
            speaker_id,
            speaker_name,
            average_embedding,
            user_id,
            sample_count=len(embeddings),
            total_duration=total_duration,
        )

        if updated:
            log.info(f"Successfully updated existing speaker: {speaker_id}")
        else:
            log.info(f"Successfully enrolled new speaker: {speaker_id}")

        return {
            "updated": updated,
            "speaker_id": speaker_id,
            "num_segments": len(embeddings),
            "num_files": len(files),
            "total_duration": round(total_duration, 2),
            "audio_saved": True,
            "saved_files": len(saved_audio_files),
        }

    except Exception as e:
        log.error(f"Error during batch enrollment: {e}")
        raise HTTPException(500, f"Batch enrollment failed: {str(e)}") from e
    finally:
        # Clean up temporary files
        for tmp_path in temp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except:
                pass


@router.post("/enroll/append")
async def enroll_append(
    files: List[UploadFile] = File(
        ..., description="Multiple audio files to append to existing speaker"
    ),
    speaker_id: str = Form(..., description="Existing speaker identifier"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Append audio segments to an existing speaker, computing weighted average embedding."""
    # Extract user_id from speaker_id
    user_id = extract_user_id_from_speaker_id(speaker_id)
    log.info(
        f"Appending to speaker: {speaker_id} (User: {user_id}) with {len(files)} files"
    )

    # First, verify the speaker exists
    db_session = get_db_session()
    try:
        existing_speaker = (
            db_session.query(Speaker)
            .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
            .first()
        )

        if not existing_speaker:
            raise HTTPException(
                404, f"Speaker {speaker_id} not found for user {user_id}"
            )

        if not existing_speaker.embedding_data:
            raise HTTPException(400, f"Speaker {speaker_id} has no existing embedding")

        # Get existing embedding and counts
        existing_embedding = np.array(
            json.loads(existing_speaker.embedding_data), dtype=np.float32
        )
        existing_count = existing_speaker.audio_sample_count or 1
        existing_duration = existing_speaker.total_audio_duration or 0.0

        log.info(
            f"Existing speaker has {existing_count} samples, {existing_duration:.2f}s duration"
        )

    finally:
        db_session.close()

    embeddings = []
    temp_paths = []
    new_total_duration = 0.0
    saved_audio_files = []

    try:
        # Process each new audio file
        for i, file in enumerate(files):
            log.info(f"Processing new file {i+1}/{len(files)}: {file.filename}")

            # Read file content
            file_content = await file.read()

            # Save to temporary file for processing
            with secure_temp_file() as tmp:
                tmp.write(file_content)
                tmp_path = Path(tmp.name)
                temp_paths.append(tmp_path)

            # Load and embed
            try:
                # Get accurate duration from original file
                audio_info = get_audio_info(str(tmp_path))
                duration = audio_info["duration_seconds"]
                new_total_duration += duration

                audio_backend = get_audio_backend()
                wav = audio_backend.load_wave(tmp_path)
                emb = await audio_backend.async_embed(wav)
                embeddings.append(emb[0])

                # Save audio file to enrollment directory
                saved_path = save_enrollment_audio(
                    user_id=user_id,
                    speaker_id=speaker_id,
                    audio_data=file_content,
                    filename=file.filename or f"append_{i+1}.wav",
                    enrollment_type="append",
                )

                # Track saved audio file info
                auth = get_auth()
                audio_file_info = {
                    "filename": saved_path.name,
                    "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
                    "duration_seconds": duration,
                    "upload_time": datetime.now().isoformat(),
                    "original_filename": file.filename,
                    "append_index": i + 1,
                    "append_operation": True,
                }
                saved_audio_files.append(audio_file_info)

                log.info(
                    f"Successfully embedded and saved new file {i+1}, duration: {duration:.2f}s"
                )
            except Exception as e:
                log.warning(f"Failed to process file {i+1}: {e}")
                continue

        if not embeddings:
            raise HTTPException(400, "No valid audio files could be processed")

        # Update manifest with appended files
        save_enrollment_manifest(user_id, speaker_id, saved_audio_files)

        # Compute average of new embeddings
        log.info(f"Computing average embedding from {len(embeddings)} new segments")
        new_embeddings_array = np.array(embeddings)
        new_average_embedding = np.mean(new_embeddings_array, axis=0)

        # Compute weighted average: (old_embedding * old_count + new_embedding * new_count) / (old_count + new_count)
        new_count = len(embeddings)
        total_count = existing_count + new_count

        weighted_embedding = (
            existing_embedding * existing_count + new_average_embedding * new_count
        ) / total_count

        # Normalize the weighted embedding
        weighted_embedding = weighted_embedding / np.linalg.norm(weighted_embedding)

        log.info(
            f"Weighted average embedding computed from {existing_count} + {new_count} = {total_count} samples"
        )

        # Update speaker in database
        updated = await db.add_speaker(
            speaker_id,
            existing_speaker.name,
            weighted_embedding,
            user_id,
            sample_count=total_count,
            total_duration=existing_duration + new_total_duration,
        )

        log.info(f"Successfully appended to speaker: {speaker_id}")

        return {
            "updated": True,
            "speaker_id": speaker_id,
            "previous_samples": existing_count,
            "new_samples": new_count,
            "total_samples": total_count,
            "previous_duration": round(existing_duration, 2),
            "new_duration": round(new_total_duration, 2),
            "total_duration": round(existing_duration + new_total_duration, 2),
            "audio_saved": True,
            "saved_files": len(saved_audio_files),
        }

    except Exception as e:
        log.error(f"Error during append enrollment: {e}")
        raise HTTPException(500, f"Append enrollment failed: {str(e)}") from e
    finally:
        # Clean up temporary files
        for tmp_path in temp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except:
                pass
