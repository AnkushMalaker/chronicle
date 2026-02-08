"""Speaker identification and diarization endpoints."""

import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from simple_speaker_recognition.api.core.utils import (
    safe_format_confidence,
    secure_temp_file,
    validate_confidence
)
from simple_speaker_recognition.core.models import (
    DiarizeAndIdentifyRequest,
    IdentifyResponse,
    SpeakerStatus
)
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker
from simple_speaker_recognition.utils.audio_processing import get_audio_info
from simple_speaker_recognition.utils.analysis import create_speaker_analysis

# These will be imported from the main service.py when we integrate
# from ..service import get_db, audio_backend

router = APIRouter()
log = logging.getLogger("speaker_service")


# Dependency functions - will be resolved during integration
async def get_db():
    """Get speaker database dependency."""
    from .. import service
    return await service.get_db()


def get_audio_backend():
    """Get audio backend."""
    from .. import service
    return service.audio_backend


class AnnotationSegment(BaseModel):
    """Annotation segment for analysis."""
    start: float
    end: float
    speaker_label: str
    audio_file_hash: Optional[str] = None


class AnalyzeSegmentsRequest(BaseModel):
    """Request model for analyzing annotation segments."""
    segments: List[AnnotationSegment]
    method: str = "umap"
    cluster_method: str = "dbscan"
    similarity_threshold: float = 0.8


class CombinedAnalysisRequest(BaseModel):
    """Request model for combined analysis of segments and enrolled speakers."""
    segments: List[AnnotationSegment]
    expected_speakers: int = 2
    method: str = "umap"
    cluster_method: str = "dbscan"
    similarity_threshold: float = 0.8


@router.post("/diarize-and-identify")
async def diarize_and_identify(
    file: UploadFile = File(..., description="Audio file for diarization and speaker identification"),
    min_duration: Optional[float] = Query(default=0.5, description="Minimum duration for speaker segments (seconds)"),
    similarity_threshold: Optional[float] = Query(default=None, description="Override default similarity threshold for identification"),
    identify_only_enrolled: bool = Query(default=False, description="Only return segments for enrolled speakers"),
    user_id: Optional[int] = Query(default=None, description="User ID to scope speaker identification to user's enrolled speakers"),
    min_speakers: Optional[int] = Query(default=None, description="Minimum number of speakers to detect"),
    max_speakers: Optional[int] = Query(default=None, description="Maximum number of speakers to detect"),
    collar: Optional[float] = Query(default=2.0, description="Collar duration (seconds) around speaker boundaries to merge segments"),
    min_duration_off: Optional[float] = Query(default=1.5, description="Minimum silence duration (seconds) before treating it as a segment boundary"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Perform speaker diarization and identify enrolled speakers in one step.
    
    This endpoint:
    1. Runs pyannote diarization to segment audio by speaker
    2. For each segment, extracts embeddings and identifies enrolled speakers
    3. Returns segments with both diarization labels and identified speaker names
    """
    log.info("Processing diarize-and-identify request")
    log.info(f"Parameters - min_duration: {min_duration}, similarity_threshold: {similarity_threshold}, identify_only_enrolled: {identify_only_enrolled}, user_id: {user_id}, min_speakers: {min_speakers}, max_speakers: {max_speakers}, collar: {collar}, min_duration_off: {min_duration_off}")
    log.info(f"File - name: {file.filename}, content_type: {file.content_type}, size: {file.size if hasattr(file, 'size') else 'unknown'}")

    # Early validation: Validate file presence
    if not file or not file.filename:
        log.error("❌ VALIDATION ERROR: No audio file provided")
        raise HTTPException(
            400,
            detail={
                "error": "validation_error",
                "message": "No audio file provided",
                "field": "file"
            }
        )

    # Read audio data once
    audio_data = await file.read()

    # Early validation: Validate non-empty
    if len(audio_data) == 0:
        log.error("❌ VALIDATION ERROR: Audio file is empty")
        raise HTTPException(
            400,
            detail={
                "error": "validation_error",
                "message": "Audio file is empty",
                "field": "file"
            }
        )

    # Resource check - verify backend is initialized
    audio_backend = get_audio_backend()
    if not audio_backend:
        log.error("❌ RESOURCE ERROR: Audio backend not initialized")
        raise HTTPException(
            503,
            detail={
                "error": "resource_error",
                "message": "Audio backend not initialized",
                "resource": "audio_backend"
            }
        )
    
    # Save to temp file for processing
    with secure_temp_file() as tmp:
        tmp.write(audio_data)
        tmp_path = Path(tmp.name)
    
    # Save audio to debug directory for analysis
    from datetime import datetime
    debug_dir = Path("/app/debug")
    if debug_dir.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_filename = f"diarize_{timestamp}_{file.filename}"
        debug_path = debug_dir / debug_filename
        debug_path.write_bytes(audio_data)
        log.info(f"Saved audio for debugging to: {debug_path}")
    
    try:
        # Step 1: Perform diarization
        log.info(f"Step 1: Performing speaker diarization on {tmp_path}")
        if min_speakers or max_speakers:
            log.info(f"Using speaker constraints: min={min_speakers}, max={max_speakers}")

        # Use audio_backend from early validation (already checked above)
        segments = await audio_backend.async_diarize(tmp_path, min_speakers=min_speakers, max_speakers=max_speakers,
                                                     collar=collar, min_duration_off=min_duration_off)
        
        # Log what PyAnnote produced
        log.info(f"PyAnnote produced {len(segments)} segments")
        for i, seg in enumerate(segments[:5]):  # Log first 5 segments for debugging
            log.info(f"  Segment {i}: speaker={seg['speaker']}, start={seg['start']:.2f}, end={seg['end']:.2f}, duration={seg['duration']:.2f}s")
        if len(segments) > 5:
            log.info(f"  ... and {len(segments) - 5} more segments")
        
        # Apply minimum duration filter if specified
        if min_duration is not None:
            original_count = len(segments)
            segments = [s for s in segments if s["duration"] >= min_duration]
            if len(segments) < original_count:
                log.info(f"Filtered out {original_count - len(segments)} segments shorter than {min_duration}s")
        
        # Step 2: Identify speakers for each segment
        log.info(f"Step 2: Identifying speakers for {len(segments)} segments")
        enhanced_segments = []
        identified_speakers = set()
        unknown_speakers = set()
        
        # Use custom threshold if provided, otherwise use default
        threshold = similarity_threshold if similarity_threshold is not None else db.similarity_thr
        
        # Get audio duration for bounds checking
        audio_info = get_audio_info(str(tmp_path))
        audio_duration = audio_info.get("duration_seconds")
        
        if audio_duration is None:
            raise ValueError("Failed to get audio duration from file")
            
        log.info(f"Audio file duration: {audio_duration:.3f}s")
        
        for i, segment in enumerate(segments):
            try:
                speaker_label = segment["speaker"]
                start_time = segment["start"]
                end_time = segment["end"]
                
                # Validate and clip segment times to audio bounds
                start_time = max(0, start_time)
                end_time = min(audio_duration, end_time)
                
                # Check if segment end exceeds audio duration
                if segment["end"] > audio_duration:
                    log.warning(f"Segment {i+1} end time {segment['end']:.3f}s exceeds audio duration {audio_duration:.3f}s, clipping to {end_time:.3f}s")
                
                duration = end_time - start_time
                
                # Skip very short segments (less than min_duration)
                if duration < (min_duration or 0.5):
                    log.debug(f"Skipping segment {i+1}: too short ({duration:.2f}s)")
                    continue
                
                # Load audio segment with clipped times
                wav = audio_backend.load_wave(tmp_path, start_time, end_time)
                
                # Generate embedding
                emb = await audio_backend.async_embed(wav)
                
                # Identify speaker with custom threshold
                found = False
                speaker_info = None
                confidence = 0.0
                
                # Try to identify speaker (UnifiedSpeakerDB handles speaker existence check internally)
                # Temporarily override threshold for this identification
                original_threshold = db.similarity_thr
                db.similarity_thr = threshold
                try:
                    found, speaker_info, confidence = await db.identify(emb, user_id=user_id)
                    confidence = validate_confidence(confidence, "diarize_and_identify")
                finally:
                    db.similarity_thr = original_threshold
                
                # Build enhanced segment
                enhanced_segment = {
                    "speaker": speaker_label,
                    "start": round(start_time, 3),
                    "end": round(end_time, 3),
                    "duration": round(duration, 3),
                    "identified_as": speaker_info["name"] if found and speaker_info else None,
                    "identified_id": speaker_info["id"] if found and speaker_info else None,
                    "confidence": round(float(confidence), 3) if confidence else 0.0,
                    "status": "identified" if found else "unknown"
                }
                
                # Track identified vs unknown speakers
                if found and speaker_info:
                    identified_speakers.add(speaker_info["name"])
                    confidence_str = safe_format_confidence(confidence, "diarization_segment")
                    log.debug(f"Segment {i+1}: Identified as {speaker_info['name']} (confidence: {confidence_str})")
                else:
                    unknown_speakers.add(speaker_label)
                    log.debug(f"Segment {i+1}: Unknown speaker {speaker_label}")
                
                # Only add segment if it's identified or we're not filtering
                if not identify_only_enrolled or found:
                    enhanced_segments.append(enhanced_segment)
                    
            except Exception as e:
                log.warning(f"Error processing segment {i+1}: {str(e)}")
                # Add segment with error status unless filtering
                if not identify_only_enrolled:
                    enhanced_segments.append({
                        "speaker": segment["speaker"],
                        "start": segment["start"],
                        "end": segment["end"],
                        "duration": segment["duration"],
                        "identified_as": None,
                        "identified_id": None,
                        "confidence": 0.0,
                        "status": "error",
                        "error": str(e)
                    })
        
        # Calculate summary statistics
        total_duration = max(s["end"] for s in segments) if segments else 0
        
        log.info(f"Diarization and identification complete - {len(identified_speakers)} identified, {len(unknown_speakers)} unknown")
        
        return {
            "segments": enhanced_segments,
            "summary": {
                "total_duration": round(total_duration, 2),
                "num_segments": len(enhanced_segments),
                "num_diarized_speakers": len(set(s["speaker"] for s in segments)),
                "identified_speakers": sorted(list(identified_speakers)),
                "unknown_speakers": sorted(list(unknown_speakers)),
                "similarity_threshold": threshold,
                "filtered": identify_only_enrolled
            }
        }
        
    except Exception as e:
        log.error(f"Error during diarize-and-identify: {e}")
        raise HTTPException(500, f"Diarize and identify failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/v1/diarize-identify-match")
async def diarize_identify_match(
    file: UploadFile = File(None, description="Audio file for diarization and word matching"),
    transcript_data: str = Form(..., description="JSON string with transcript words and text"),
    user_id: Optional[int] = Form(default=None, description="User ID for speaker identification"),
    conversation_id: Optional[str] = Form(default=None, description="Conversation ID to fetch audio from backend"),
    backend_token: Optional[str] = Form(default=None, description="JWT token for backend API authentication"),
    min_duration: float = Form(default=0.5, description="Minimum segment duration in seconds"),
    similarity_threshold: float = Form(default=0.15, description="Speaker similarity threshold"),
    min_speakers: Optional[int] = Form(default=None, description="Minimum number of speakers to detect"),
    max_speakers: Optional[int] = Form(default=None, description="Maximum number of speakers to detect"),
    collar: float = Form(default=2.0, description="Collar duration (seconds) around speaker boundaries to merge segments"),
    min_duration_off: float = Form(default=1.5, description="Minimum silence duration (seconds) before treating it as a segment boundary"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Diarize audio, identify speakers, and match transcript words to speaker segments.

    This endpoint supports two modes:
    1. File upload: Pass audio file directly
    2. Conversation mode: Pass conversation_id + backend_token to fetch audio from Chronicle backend

    The endpoint:
    1. Uses internal pyannote for speaker diarization (with automatic chunking for large files)
    2. Identifies enrolled speakers for each segment
    3. Matches transcript words to diarization segments by time overlap
    4. Returns complete segments with text and speaker identification

    The transcript_data should be a JSON string containing:
    {
        "words": [{"word": "hello", "start": 1.23, "end": 1.45}, ...],
        "text": "full transcript text"
    }

    Maximum audio duration: 2 hours (7200 seconds)
    Files longer than max_diarize_duration (default 60s) are automatically chunked
    """
    log.info(f"Processing diarize-identify-match request")
    log.info(f"Mode: {'conversation' if conversation_id else 'file upload'}")
    log.info(f"Parameters - user_id: {user_id}, min_duration: {min_duration}, similarity_threshold: {similarity_threshold}, min_speakers: {min_speakers}, max_speakers: {max_speakers}, collar: {collar}, min_duration_off: {min_duration_off}")
    log.info(f"Transcript data length: {len(transcript_data) if transcript_data else 0}")

    # Validate: must provide either file OR conversation_id
    if not file and not conversation_id:
        raise HTTPException(400, "Must provide either audio file or conversation_id")
    if file and conversation_id:
        raise HTTPException(400, "Cannot provide both audio file and conversation_id - choose one mode")
    if conversation_id and not backend_token:
        raise HTTPException(400, "backend_token required when using conversation_id")

    # Early validation: Parse transcript_data FIRST (fail fast if invalid)
    try:
        transcript = json.loads(transcript_data)
        words = transcript.get("words", [])
    except json.JSONDecodeError as e:
        error_msg = f"Invalid transcript_data JSON: {str(e)}"
        log.error(f"❌ VALIDATION ERROR: {error_msg}")
        raise HTTPException(
            400,
            detail={
                "error": "validation_error",
                "message": error_msg,
                "field": "transcript_data"
            }
        ) from e

    if not words:
        error_msg = f"No words found in transcript_data (transcript keys: {list(transcript.keys())}, words type: {type(words)})"
        log.error(f"❌ VALIDATION ERROR: {error_msg}")
        raise HTTPException(
            400,
            detail={
                "error": "validation_error",
                "message": error_msg,
                "field": "transcript_data.words"
            }
        )

    # Resource check - verify model is loaded before processing
    audio_backend = get_audio_backend()
    if not audio_backend or not hasattr(audio_backend, 'async_diarize'):
        log.error("❌ RESOURCE ERROR: Diarization model not loaded")
        raise HTTPException(
            503,
            detail={
                "error": "resource_error",
                "message": "Diarization model not loaded",
                "resource": "diarization_model"
            }
        )

    # Get settings for chunking configuration
    from simple_speaker_recognition.api.service import auth as settings
    max_diarize_duration = settings.max_diarize_duration  # Default 60 seconds
    diarize_chunk_overlap = settings.diarize_chunk_overlap  # Default 5 seconds
    MAX_AUDIO_DURATION = 7200  # 2 hours hard limit

    # Mode 1: Conversation mode - fetch audio from backend
    if conversation_id:
        from simple_speaker_recognition.core.backend_client import BackendClient

        backend_client = BackendClient(settings.backend_api_url)
        try:
            # Get conversation metadata
            metadata = await backend_client.get_conversation_metadata(conversation_id, backend_token)
            total_duration = metadata.get('duration')
            if total_duration is None:
                raise HTTPException(400, "Conversation metadata missing duration field")

            log.info(f"Conversation {conversation_id[:12]}: duration={total_duration:.1f}s")

            # Validate: 2 hour maximum
            if total_duration > MAX_AUDIO_DURATION:
                raise HTTPException(400, f"Audio duration {total_duration:.1f}s exceeds maximum allowed duration of {MAX_AUDIO_DURATION}s (2 hours)")

            # Fetch full audio from backend
            log.info(f"Fetching audio from backend for conversation {conversation_id[:12]}")
            wav_bytes = await backend_client.get_audio_segment(
                conversation_id,
                backend_token,
                start=0.0,
                duration=total_duration
            )

            # Write to temp file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_file.write(wav_bytes)
                tmp_path = Path(tmp_file.name)
        finally:
            await backend_client.close()

    # Mode 2: File upload mode
    else:
        # Create temporary file from upload
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(await file.read())
            tmp_path = Path(tmp_file.name)

        # Get audio duration for validation
        from simple_speaker_recognition.utils.audio_processing import get_audio_info
        audio_info = get_audio_info(str(tmp_path))
        total_duration = audio_info.get('duration_seconds', 0)

        log.info(f"Uploaded file: {file.filename}, duration={total_duration:.1f}s")

        # Validate: 2 hour maximum
        if total_duration > MAX_AUDIO_DURATION:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(400, f"Audio duration {total_duration:.1f}s exceeds maximum allowed duration of {MAX_AUDIO_DURATION}s (2 hours)")

    try:
        # Step 1: Perform diarization (chunking happens automatically inside if needed)
        log.info(f"Performing speaker diarization on {tmp_path}")
        if min_speakers or max_speakers:
            log.info(f"Using speaker constraints: min={min_speakers}, max={max_speakers}")

        # Use audio_backend from early validation (already checked above)
        diarization_segments = await audio_backend.async_diarize(
            tmp_path,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            collar=collar,
            min_duration_off=min_duration_off,
            max_duration=max_diarize_duration,
            chunk_overlap=diarize_chunk_overlap
        )
        
        # Apply minimum duration filter
        if min_duration > 0:
            original_count = len(diarization_segments)
            diarization_segments = [s for s in diarization_segments if s["duration"] >= min_duration]
            if len(diarization_segments) < original_count:
                log.info(f"Filtered out {original_count - len(diarization_segments)} segments shorter than {min_duration}s")
        
        # Step 2: Identify speakers for each segment
        enhanced_segments = []
        for segment in diarization_segments:
            speaker_label = segment["speaker"]
            start_time = segment["start"]
            end_time = segment["end"]
            
            # Extract audio for this segment using correct method
            segment_audio = audio_backend.load_wave(tmp_path, start_time, end_time)
            
            # Check if we can identify this speaker
            speaker_info = None
            confidence = 0.0
            found = False
            if user_id:
                # Generate embedding for this segment
                emb = await audio_backend.async_embed(segment_audio)
                
                # Identify speaker using the database
                found, speaker_info, confidence = await db.identify(emb, user_id=user_id)
            
            # Step 3: Match transcript words to this segment
            segment_words = []
            for word in words:
                word_start = word.get("start", 0.0)
                word_end = word.get("end", 0.0)
                word_mid = (word_start + word_end) / 2

                # Word belongs to this segment if its midpoint is within range
                if start_time <= word_mid <= end_time:
                    segment_words.append(word)  # Keep full word object with timestamps

            # Create segment with matched text
            segment_text = " ".join(w.get("word", "") for w in segment_words).strip()
            
            if speaker_info and confidence >= similarity_threshold:
                # Identified speaker
                enhanced_segments.append({
                    "text": segment_text,
                    "start": round(start_time, 3),
                    "end": round(end_time, 3),
                    "speaker": speaker_label,
                    "identified_as": speaker_info["name"],
                    "speaker_id": speaker_info["id"],
                    "confidence": round(float(confidence), 3),
                    "status": "identified",
                    "words": segment_words  # Include word-level timestamps
                })
            else:
                # Unknown speaker
                enhanced_segments.append({
                    "text": segment_text,
                    "start": round(start_time, 3),
                    "end": round(end_time, 3),
                    "speaker": speaker_label,
                    "identified_as": None,
                    "speaker_id": None,
                    "confidence": round(float(confidence), 3) if confidence else 0.0,
                    "status": "unknown",
                    "words": segment_words  # Include word-level timestamps
                })
        
        # Create summary
        identified_speakers = list(set(
            s["identified_as"] for s in enhanced_segments 
            if s["identified_as"]
        ))
        unknown_speakers = list(set(
            s["speaker"] for s in enhanced_segments 
            if not s["identified_as"]
        ))
        
        response = {
            "segments": enhanced_segments,
            "summary": {
                "total_segments": len(enhanced_segments),
                "identified_speakers": identified_speakers,
                "unknown_speakers": unknown_speakers,
                "similarity_threshold": similarity_threshold,
                "processing_mode": "diarize_identify_match"
            }
        }
        
        log.info(f"Diarize-identify-match complete: {len(enhanced_segments)} segments, "
                f"{len(identified_speakers)} identified speakers")
        return response
        
    finally:
        # Clean up temporary file
        tmp_path.unlink(missing_ok=True)


@router.post("/plain-diarize-and-identify")
async def plain_diarize_and_identify(
    file: UploadFile = File(..., description="Audio file for plain diarization and speaker identification"),
    min_duration: Optional[float] = Form(default=0.5, description="Minimum duration for speaker segments (seconds)"),
    similarity_threshold: Optional[float] = Form(default=None, description="Override default similarity threshold for identification"),
    identify_only_enrolled: bool = Form(default=False, description="Only return segments for enrolled speakers"),
    user_id: Optional[int] = Form(default=None, description="User ID to scope speaker identification to user's enrolled speakers"),
    min_speakers: Optional[int] = Form(default=None, description="Minimum number of speakers to detect"),
    max_speakers: Optional[int] = Form(default=None, description="Maximum number of speakers to detect"),
    collar: Optional[float] = Form(default=2.0, description="Collar duration (seconds) around speaker boundaries to merge segments"),
    min_duration_off: Optional[float] = Form(default=1.5, description="Minimum silence duration (seconds) before treating it as a segment boundary"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Plain diarization and speaker identification without transcription.
    
    This is an alias for the standard diarize-and-identify endpoint,
    provided for frontend compatibility with different processing modes.
    """
    log.info("Processing plain-diarize-and-identify request (redirecting to standard diarize-and-identify)")
    
    # Simply call the existing diarize_and_identify function with the same parameters
    return await diarize_and_identify(file, min_duration, similarity_threshold, identify_only_enrolled, user_id, min_speakers, max_speakers, collar, min_duration_off, db)


@router.post("/identify", response_model=IdentifyResponse)
async def identify(
    file: UploadFile = File(..., description="Audio file for speaker identification"),
    similarity_threshold: Optional[float] = Form(default=None, description="Override default similarity threshold for identification"),
    user_id: Optional[int] = Form(default=None, description="User ID to scope speaker identification to user's enrolled speakers"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Identify the speaker in an audio file.
    
    This endpoint is optimized for real-time processing:
    1. Assumes the audio contains speech from a single speaker
    2. Extracts embedding from the entire audio chunk
    3. Identifies the enrolled speaker
    4. Returns a single identification result
    
    Designed for use with utterance boundaries in real-time transcription.
    """
    log.info("Processing identify request")
    
    with secure_temp_file() as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
        
        # Debug: Copy WAV file to debug directory
        try:
            debug_dir = Path("/app/debug")
            if not debug_dir.exists():
                log.error(f"Debug directory does not exist, creating: {debug_dir}")
            
            # Create filename with timestamp and original filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # milliseconds
            original_name = getattr(file, 'filename', 'utterance.wav') or 'utterance.wav'
            debug_filename = f"{timestamp}_{original_name}"
            debug_path = debug_dir / debug_filename
            
            # Copy the temp file to debug location
            shutil.copy2(tmp_path, debug_path)
            
            log.info(f"🐛 [DEBUG] WAV file dumped to: {debug_path}")
        except Exception as e:
            log.warning(f"Failed to dump debug WAV file: {e}")
    
    try:
        # Get audio info for duration
        audio_info = get_audio_info(str(tmp_path))
        duration = audio_info.get('duration_seconds')
        
        if duration is None:
            raise ValueError("Failed to get audio duration from file")
        
        log.info(f"Processing audio: {duration:.2f}s duration")
        
        # Load the entire audio file (no segmentation needed)
        audio_backend = get_audio_backend()
        wav = audio_backend.load_wave(tmp_path)
        
        # Generate embedding for the entire utterance
        emb = await audio_backend.async_embed(wav)
        
        # Use custom threshold if provided, otherwise use default
        threshold = similarity_threshold if similarity_threshold is not None else db.similarity_thr
        
        # Identify speaker with custom threshold
        found = False
        speaker_info = None
        confidence = 0.0
        
        # Temporarily override threshold for this identification
        original_threshold = db.similarity_thr
        db.similarity_thr = threshold
        try:
            found, speaker_info, confidence = await db.identify(emb, user_id=user_id)
            confidence = validate_confidence(confidence, "speaker_identification")
        finally:
            db.similarity_thr = original_threshold
        
        # Build response
        if found and speaker_info:
            confidence_str = safe_format_confidence(confidence, "speaker_identification")
            log.info(f"Speaker identified as {speaker_info['name']} (confidence: {confidence_str})")
            
            return IdentifyResponse(
                found=True,
                speaker_id=speaker_info["id"],
                speaker_name=speaker_info["name"],
                confidence=round(float(confidence), 3),
                status=SpeakerStatus.IDENTIFIED,
                similarity_threshold=threshold,
                duration=round(duration, 3)
            )
        else:
            log.info(f"Speaker not identified (confidence: {confidence:.3f} < threshold: {threshold:.3f})")
            
            return IdentifyResponse(
                found=False,
                speaker_id=None,
                speaker_name=None,
                confidence=round(float(confidence), 3),
                status=SpeakerStatus.UNKNOWN,
                similarity_threshold=threshold,
                duration=round(duration, 3)
            )
        
    except Exception as e:
        log.error(f"Error during speaker identification: {e}")
        raise HTTPException(500, f"Speaker identification failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/annotations/analyze-segments")
async def analyze_annotation_segments(
    audio_file: UploadFile = File(..., description="Audio file containing the segments"),
    segments: str = Form(..., description="JSON string of segments to analyze"),
    method: str = Form(default="umap", description="Dimensionality reduction method"),
    cluster_method: str = Form(default="dbscan", description="Clustering method"),
    similarity_threshold: float = Form(default=0.8, description="Similarity threshold"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Analyze speaker embedding clustering for annotation segments.
    
    This endpoint extracts embeddings from specific segments in an audio file
    and performs clustering analysis to help visualize speaker separation.
    """
    import json
    
    # Parse segments JSON
    try:
        segments_data = json.loads(segments)
        request_segments = [AnnotationSegment(**seg) for seg in segments_data]
    except Exception as e:
        raise HTTPException(400, f"Invalid segments JSON: {str(e)}")
    
    log.info(f"Processing segment analysis request for {len(request_segments)} segments")
    
    if len(request_segments) == 0:
        return {
            "status": "success",
            "message": "No segments provided",
            "visualization": {
                "speakers": [],
                "embeddings_2d": [],
                "embeddings_3d": [],
                "cluster_labels": [],
                "colors": []
            },
            "clustering": {"n_clusters": 0, "method": cluster_method},
            "similar_speakers": [],
            "quality_metrics": {"n_speakers": 0},
            "parameters": {
                "reduction_method": method,
                "cluster_method": cluster_method,
                "similarity_threshold": similarity_threshold
            }
        }
    
    with secure_temp_file() as tmp:
        tmp.write(await audio_file.read())
        tmp_path = Path(tmp.name)
    
    try:
        # Extract embeddings for each segment
        audio_backend = get_audio_backend()
        embeddings_dict = {}
        
        for i, segment in enumerate(request_segments):
            try:
                # Load audio segment
                wav = audio_backend.load_wave(tmp_path, segment.start, segment.end)
                
                # Generate embedding
                emb = await audio_backend.async_embed(wav)
                
                # Debug: Log embedding shape and type
                log.info(f"Raw embedding shape: {emb.shape if hasattr(emb, 'shape') else type(emb)}, dtype: {emb.dtype if hasattr(emb, 'dtype') else 'unknown'}")
                
                # Ensure embedding is properly shaped (should be 1D)
                if hasattr(emb, 'shape') and len(emb.shape) > 1:
                    emb = emb.flatten()
                elif not hasattr(emb, 'shape'):
                    emb = np.array(emb).flatten()
                
                log.info(f"Processed embedding shape: {emb.shape}")
                
                # Create unique identifier for this segment
                segment_id = f"{segment.speaker_label}_seg_{i}_{segment.start:.2f}s"
                embeddings_dict[segment_id] = emb
                
                log.debug(f"Extracted embedding for segment {i}: {segment.speaker_label} ({segment.start:.2f}s - {segment.end:.2f}s)")
                
            except Exception as e:
                log.warning(f"Failed to extract embedding for segment {i}: {e}")
                continue
        
        if not embeddings_dict:
            return {
                "status": "error",
                "message": "Failed to extract embeddings from any segments",
                "error": "No valid segments could be processed"
            }
        
        # Perform analysis on extracted embeddings
        log.info(f"Analyzing {len(embeddings_dict)} segment embeddings")
        analysis_result = create_speaker_analysis(
            embeddings_dict=embeddings_dict,
            method=method,
            cluster_method=cluster_method,
            similarity_threshold=similarity_threshold
        )
        
        if analysis_result.get("status") == "failed":
            log.error(f"Analysis failed: {analysis_result.get('error')}")
            return {
                "status": "error",
                "message": "Embedding analysis failed",
                "error": analysis_result.get("error")
            }
        
        # Add metadata about segments
        analysis_result["segment_info"] = {
            "total_segments": len(request_segments),
            "processed_segments": len(embeddings_dict),
            "unique_speakers": list(set(seg.speaker_label for seg in request_segments)),
            "total_duration": sum(seg.end - seg.start for seg in request_segments)
        }
        
        log.info(f"Segment analysis completed successfully for {len(embeddings_dict)} segments")
        return analysis_result
        
    except Exception as e:
        log.error(f"Error during segment analysis: {e}")
        raise HTTPException(500, f"Segment analysis failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/annotations/analyze-with-enrolled")
async def analyze_segments_with_enrolled_speakers(
    audio_file: UploadFile = File(..., description="Audio file containing the segments"),
    segments: str = Form(..., description="JSON string of segments to analyze"),
    expected_speakers: int = Form(default=2, description="Expected number of speakers in audio"),
    user_id: Optional[int] = Form(default=None, description="User ID to get enrolled speakers"),
    method: str = Form(default="umap", description="Dimensionality reduction method"),
    cluster_method: str = Form(default="dbscan", description="Clustering method"),
    similarity_threshold: float = Form(default=0.8, description="Similarity threshold"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    Combined analysis of annotation segments and enrolled speakers.
    
    This endpoint:
    1. Extracts embeddings from annotation segments
    2. Gets embeddings from enrolled speakers  
    3. Combines both in unified visualization
    4. Suggests optimal threshold based on separation
    """
    import json
    
    # Parse segments JSON
    try:
        segments_data = json.loads(segments)
        request_segments = [AnnotationSegment(**seg) for seg in segments_data]
    except Exception as e:
        raise HTTPException(400, f"Invalid segments JSON: {str(e)}")
    
    log.info(f"Processing combined analysis for {len(request_segments)} segments with {expected_speakers} expected speakers")
    
    if len(request_segments) == 0:
        return {
            "status": "error",
            "message": "No segments provided",
            "error": "No segments to analyze"
        }
    
    with secure_temp_file() as tmp:
        tmp.write(await audio_file.read())
        tmp_path = Path(tmp.name)
    
    try:
        # Get audio duration for bounds checking
        audio_info = get_audio_info(tmp_path)
        audio_duration = audio_info.get("duration_seconds")
        
        if audio_duration is None:
            raise ValueError("Failed to get audio duration from file")
            
        log.info(f"Audio file duration: {audio_duration:.3f}s")
        
        # Extract embeddings from annotation segments
        audio_backend = get_audio_backend()
        segment_embeddings_dict = {}
        
        for i, segment in enumerate(request_segments):
            try:
                # Validate and clip segment times
                segment_start = max(0, segment.start)
                segment_end = min(audio_duration, segment.end)
                
                # Check if segment end exceeds audio duration
                if segment.end > audio_duration:
                    log.warning(f"Segment {i} end time {segment.end:.3f}s exceeds audio duration {audio_duration:.3f}s, clipping to {segment_end:.3f}s")
                
                duration = segment_end - segment_start
                if duration <= 0:
                    log.warning(f"Invalid segment duration for segment {i}: {duration}s")
                    continue
                
                log.info(f"Processing segment {i}: {segment.speaker_label} ({segment_start:.2f}s - {segment_end:.2f}s, duration: {duration:.2f}s)")
                
                # Load audio segment with clipped times
                wav = audio_backend.load_wave(tmp_path, segment_start, segment_end)
                log.debug(f"Loaded audio segment shape: {wav.shape if hasattr(wav, 'shape') else 'unknown'}")
                
                # Generate embedding
                emb = await audio_backend.async_embed(wav)
                log.debug(f"Generated embedding shape: {emb.shape if hasattr(emb, 'shape') else 'unknown'}")
                
                # Ensure embedding is properly shaped (should be 1D)
                if hasattr(emb, 'shape') and len(emb.shape) > 1:
                    emb = emb.flatten()
                elif not hasattr(emb, 'shape'):
                    emb = np.array(emb).flatten()
                
                # Create identifier for this segment using clipped times
                segment_id = f"segment_{i}_{segment.speaker_label}_{segment_start:.2f}s"
                segment_embeddings_dict[segment_id] = emb
                
                log.info(f"Successfully extracted embedding for segment {i}: {segment.speaker_label} ({segment_start:.2f}s - {segment_end:.2f}s), embedding shape: {emb.shape}")
                
            except Exception as e:
                log.error(f"Failed to extract embedding for segment {i}: {e}", exc_info=True)
                continue
        
        if not segment_embeddings_dict:
            return {
                "status": "error",
                "message": "Failed to extract speaker embeddings from audio segments",
                "error": "Could not generate embeddings - please check if the audio file is valid and segments have sufficient audio data",
                "details": {
                    "segments_provided": len(request_segments),
                    "segments_processed": 0,
                    "hint": "This usually happens when: 1) Audio file is corrupted, 2) Segments are too short or silent, 3) Audio format is unsupported"
                }
            }
        
        # Get enrolled speaker embeddings
        enrolled_embeddings_dict = {}
        if user_id:
            db_session = get_db_session()
            try:
                enrolled_speakers = db_session.query(Speaker).filter(Speaker.user_id == user_id).all()
                
                for speaker in enrolled_speakers:
                    if speaker.embedding_data:
                        try:
                            embedding = np.array(json.loads(speaker.embedding_data), dtype=np.float32)
                            enrolled_embeddings_dict[f"enrolled_{speaker.id}_{speaker.name}"] = embedding
                        except (json.JSONDecodeError, ValueError) as e:
                            log.warning(f"Invalid embedding data for speaker {speaker.id}: {e}")
                            continue
            finally:
                db_session.close()
        
        # Combine all embeddings for unified analysis
        all_embeddings_dict = {**segment_embeddings_dict, **enrolled_embeddings_dict}
        
        log.info(f"Combined analysis: {len(segment_embeddings_dict)} segments + {len(enrolled_embeddings_dict)} enrolled speakers = {len(all_embeddings_dict)} total embeddings")
        
        # Perform unified analysis
        analysis_result = create_speaker_analysis(
            embeddings_dict=all_embeddings_dict,
            method=method,
            cluster_method=cluster_method,
            similarity_threshold=similarity_threshold
        )
        
        if analysis_result.get("status") == "failed":
            log.error(f"Combined analysis failed: {analysis_result.get('error')}")
            return {
                "status": "error", 
                "message": "Combined analysis failed",
                "error": analysis_result.get("error")
            }
        
        # Add metadata and smart suggestions
        analysis_result["segment_info"] = {
            "total_segments": len(request_segments),
            "processed_segments": len(segment_embeddings_dict),
            "enrolled_speakers": len(enrolled_embeddings_dict),
            "expected_speakers": expected_speakers,
            "analysis_type": "combined"
        }
        
        # Calculate smart threshold suggestion (basic implementation)
        # TODO: Implement more sophisticated algorithm
        segment_count = len(segment_embeddings_dict)
        enrolled_count = len(enrolled_embeddings_dict)
        
        if enrolled_count == 0:
            suggested_threshold = 0.9  # Very high since no enrolled speakers
            suggestion_reason = "No enrolled speakers found - use high threshold to avoid false matches"
        elif segment_count > expected_speakers * 2:
            suggested_threshold = 0.6  # Medium-high for many segments
            suggestion_reason = f"Many segments detected - recommend higher threshold for precision"
        else:
            suggested_threshold = 0.4  # Standard threshold
            suggestion_reason = "Standard threshold based on segment count"
        
        analysis_result["smart_suggestion"] = {
            "suggested_threshold": suggested_threshold,
            "confidence": "medium",
            "reasoning": suggestion_reason,
            "detected_clusters": analysis_result.get("clustering", {}).get("n_clusters", 0),
            "expected_speakers": expected_speakers
        }
        
        # Add embedding type information for visualization  
        analysis_result["embedding_types"] = {
            "segments": list(segment_embeddings_dict.keys()),
            "enrolled": list(enrolled_embeddings_dict.keys())
        }
        
        log.info(f"Combined analysis completed successfully: {len(segment_embeddings_dict)} segments, {len(enrolled_embeddings_dict)} enrolled")
        return analysis_result
        
    except Exception as e:
        log.error(f"Error during combined analysis: {e}")
        raise HTTPException(500, f"Combined analysis failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)