"""
Speaker recognition client for integrating with the speaker recognition service.

This module provides an optional integration with the speaker recognition service
to enhance transcripts with actual speaker names instead of generic labels.

Configuration is managed via config.yml (speaker_recognition section).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp import ClientConnectorError

from advanced_omi_backend.model_registry import get_models_registry

logger = logging.getLogger(__name__)


class SpeakerRecognitionClient:
    """Client for communicating with the speaker recognition service."""

    def __init__(self, service_url: Optional[str] = None):
        """
        Initialize the speaker recognition client.

        Configuration is read from config.yml (speaker_recognition section).
        The 'enabled' flag controls whether speaker recognition is active.

        Args:
            service_url: URL of the speaker recognition service (e.g., http://speaker-service:8085)
                        If not provided, uses config.yml service_url or SPEAKER_SERVICE_URL env var
        """
        # Load speaker recognition config from config.yml
        registry = get_models_registry()
        if not registry or not registry.speaker_recognition:
            # No config found, default to disabled
            self.enabled = False
            self.service_url = None
            logger.info("Speaker recognition client disabled (no configuration found)")
            return

        speaker_config = registry.speaker_recognition
        if not speaker_config.get("enabled", True):
            # Disabled in config
            self.enabled = False
            self.service_url = None
            logger.info("Speaker recognition client disabled (config.yml enabled=false)")
            return

        # Enabled - determine URL (priority: param > config > env var)
        self.service_url = (
            service_url
            or speaker_config.get("service_url")
            or os.getenv("SPEAKER_SERVICE_URL")
        )
        self.enabled = bool(self.service_url)

        if self.enabled:
            logger.info(f"Speaker recognition client initialized with URL: {self.service_url}")
        else:
            logger.info("Speaker recognition client disabled (no service URL configured)")

    async def diarize_identify_match(
        self, audio_path: str, transcript_data: Dict, user_id: Optional[str] = None
    ) -> Dict:
        """
        Perform diarization, speaker identification, and word-to-speaker matching.
        Routes to appropriate endpoint based on diarization source configuration.

        Args:
            audio_path: Path to the audio file
            transcript_data: Dict containing words array and text from transcription
            user_id: Optional user ID for speaker identification

        Returns:
            Dictionary containing segments with matched text and speaker identification
        """
        if not self.enabled:
            logger.info(f"🎤 Speaker recognition disabled, returning empty result")
            return {}

        try:
            logger.info(f"🎤 Identifying speakers for {audio_path}")

            # Read diarization source from existing config system
            from advanced_omi_backend.config import load_diarization_settings_from_file
            config = load_diarization_settings_from_file()
            diarization_source = config.get("diarization_source", "pyannote")

            async with aiohttp.ClientSession() as session:
                # Prepare the audio file for upload
                with open(audio_path, "rb") as audio_file:
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        "file", audio_file, filename=Path(audio_path).name, content_type="audio/wav"
                    )
                    
                    if diarization_source == "deepgram":
                        # DEEPGRAM DIARIZATION PATH: We EXPECT transcript has speaker info from Deepgram
                        # Only need speaker identification of existing segments
                        logger.info("Using Deepgram diarization path - transcript should have speaker segments, identifying speakers")
                        
                        # TODO: Implement proper speaker identification for Deepgram segments
                        # For now, use diarize-identify-match as fallback until we implement segment identification
                        logger.warning("Deepgram segment identification not yet implemented, using diarize-identify-match as fallback")
                        
                        form_data.add_field("transcript_data", json.dumps(transcript_data))
                        form_data.add_field("user_id", "1")  # TODO: Implement proper user mapping
                        form_data.add_field("similarity_threshold", str(config.get("similarity_threshold", 0.15)))
                        form_data.add_field("min_duration", str(config.get("min_duration", 0.5)))
                        
                        # Use /v1/diarize-identify-match endpoint as fallback
                        endpoint = "/v1/diarize-identify-match"
                        
                    else:  # pyannote (default)
                        # PYANNOTE PATH: Backend has transcript, need diarization + speaker identification
                        logger.info("Using Pyannote path - diarizing backend transcript and identifying speakers")
                        
                        # Send existing transcript for diarization and speaker matching
                        form_data.add_field("transcript_data", json.dumps(transcript_data))
                        form_data.add_field("user_id", "1")  # TODO: Implement proper user mapping
                        form_data.add_field("similarity_threshold", str(config.get("similarity_threshold", 0.15)))
                        
                        # Add pyannote diarization parameters
                        form_data.add_field("min_duration", str(config.get("min_duration", 0.5)))
                        form_data.add_field("collar", str(config.get("collar", 2.0)))
                        form_data.add_field("min_duration_off", str(config.get("min_duration_off", 1.5)))
                        if config.get("min_speakers"):
                            form_data.add_field("min_speakers", str(config.get("min_speakers")))
                        if config.get("max_speakers"):
                            form_data.add_field("max_speakers", str(config.get("max_speakers")))
                        
                        # Use /v1/diarize-identify-match endpoint for backend integration
                        endpoint = "/v1/diarize-identify-match"

                    # Make the request to the consolidated endpoint
                    request_url = f"{self.service_url}{endpoint}"
                    logger.info(f"🎤 DEBUG: Making request to speaker service URL: {request_url}")

                    async with session.post(
                        request_url,
                        data=form_data,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as response:
                        logger.info(f"🎤 Speaker service response status: {response.status}")

                        if response.status != 200:
                            response_text = await response.text()
                            logger.error(
                                f"🎤 ❌ Speaker service returned status {response.status}: {response_text}"
                            )
                            return {}

                        result = await response.json()

                        # Log basic result info
                        num_segments = len(result.get("segments", []))
                        logger.info(f"🎤 Speaker recognition returned {num_segments} segments")

                        return result

        except ClientConnectorError as e:
            logger.error(f"🎤 Failed to connect to speaker recognition service: {e}")
            return {"error": "connection_failed", "message": str(e), "segments": []}
        except asyncio.TimeoutError as e:
            logger.error(f"🎤 Timeout connecting to speaker recognition service: {e}")
            return {"error": "timeout", "message": str(e), "segments": []}
        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Client error during speaker recognition: {e}")
            return {"error": "client_error", "message": str(e), "segments": []}
        except Exception as e:
            logger.error(f"🎤 Error during speaker recognition: {e}")
            return {"error": "unknown_error", "message": str(e), "segments": []}

    async def diarize_and_identify(
        self, audio_path: str, words: None, user_id: Optional[str] = None  # NOT IMPLEMENTED
    ) -> Dict:
        """
        Perform diarization and speaker identification using the speaker recognition service.

        Args:
            audio_path: Path to the audio file
            words: Optional word-level data from transcription provider (for hints)
            user_id: Optional user ID for speaker identification

        Returns:
            Dictionary containing segments with speaker identification results
        """
        if words:
            logger.warning("Words parameter is not implemented yet")

        if not self.enabled:
            logger.warning("🎤 [DIARIZE] Speaker recognition is disabled")
            return {}

        try:
            logger.info(f"🎤 [DIARIZE] Starting diarization and identification for {audio_path}")

            # Verify file exists and get info
            if not os.path.exists(audio_path):
                logger.error(f"🎤 [DIARIZE] ❌ Audio file does not exist: {audio_path}")
                return {}

            file_size = os.path.getsize(audio_path)
            logger.info(f"🎤 [DIARIZE] Audio file size: {file_size} bytes")

            # Call the speaker recognition service
            async with aiohttp.ClientSession() as session:
                # Prepare the audio file for upload
                with open(audio_path, "rb") as audio_file:
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        "file", audio_file, filename=Path(audio_path).name, content_type="audio/wav"
                    )
                    # Get current diarization settings from config
                    from advanced_omi_backend.config import load_diarization_settings_from_file

                    diarization_settings = load_diarization_settings_from_file()

                    # Add all diarization parameters for the diarize-and-identify endpoint
                    min_duration = diarization_settings.get("min_duration", 0.5)
                    similarity_threshold = diarization_settings.get("similarity_threshold", 0.15)
                    collar = diarization_settings.get("collar", 2.0)
                    min_duration_off = diarization_settings.get("min_duration_off", 1.5)

                    form_data.add_field("min_duration", str(min_duration))
                    form_data.add_field("similarity_threshold", str(similarity_threshold))
                    form_data.add_field("collar", str(collar))
                    form_data.add_field("min_duration_off", str(min_duration_off))

                    if diarization_settings.get("min_speakers"):
                        form_data.add_field("min_speakers", str(diarization_settings["min_speakers"]))
                    if diarization_settings.get("max_speakers"):
                        form_data.add_field("max_speakers", str(diarization_settings["max_speakers"]))

                    form_data.add_field("identify_only_enrolled", "false")
                    # TODO: Implement proper user mapping between MongoDB ObjectIds and speaker service integer IDs
                    # For now, hardcode to admin user (ID=1) since speaker service expects integer user_id
                    form_data.add_field("user_id", "1")

                    endpoint_url = f"{self.service_url}/diarize-and-identify"
                    logger.info(f"🎤 [DIARIZE] Calling speaker service: {endpoint_url}")
                    logger.info(
                        f"🎤 [DIARIZE] Parameters: min_duration={min_duration}, "
                        f"similarity_threshold={similarity_threshold}, collar={collar}, "
                        f"min_duration_off={min_duration_off}, user_id=1"
                    )

                    # Make the request
                    async with session.post(
                        endpoint_url,
                        data=form_data,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as response:
                        logger.info(f"🎤 [DIARIZE] Response status: {response.status}")

                        if response.status != 200:
                            response_text = await response.text()
                            logger.warning(
                                f"🎤 [DIARIZE] ❌ Speaker recognition service returned status {response.status}: {response_text}"
                            )
                            return {}

                        result = await response.json()
                        segments_count = len(result.get('segments', []))
                        logger.info(f"🎤 [DIARIZE] ✅ Speaker service returned {segments_count} segments")

                        # Log details about identified speakers
                        if segments_count > 0:
                            identified_names = set()
                            for seg in result.get('segments', []):
                                identified_as = seg.get('identified_as')
                                if identified_as and identified_as != 'Unknown':
                                    identified_names.add(identified_as)

                            if identified_names:
                                logger.info(f"🎤 [DIARIZE] Identified speakers in segments: {identified_names}")
                            else:
                                logger.warning(f"🎤 [DIARIZE] No identified speakers found in {segments_count} segments")

                        return result

        except ClientConnectorError as e:
            logger.error(f"🎤 [DIARIZE] ❌ Failed to connect to speaker recognition service at {self.service_url}: {e}")
            return {"error": "connection_failed", "message": str(e), "segments": []}
        except asyncio.TimeoutError as e:
            logger.error(f"🎤 [DIARIZE] ❌ Timeout connecting to speaker recognition service: {e}")
            return {"error": "timeout", "message": str(e), "segments": []}
        except aiohttp.ClientError as e:
            logger.warning(f"🎤 [DIARIZE] ❌ Client error during speaker recognition: {e}")
            return {"error": "client_error", "message": str(e), "segments": []}
        except Exception as e:
            logger.error(f"🎤 [DIARIZE] ❌ Error during speaker diarization and identification: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"error": "unknown_error", "message": str(e), "segments": []}

    async def identify_speakers(self, audio_path: str, segments: List[Dict]) -> Dict[str, str]:
        """
        Identify speakers in audio segments using the speaker recognition service.

        Args:
            audio_path: Path to the audio file
            segments: List of transcript segments with speaker labels

        Returns:
            Dictionary mapping generic speaker labels to identified names
            e.g., {"Speaker 0": "ankush", "Speaker 1": "unknown_speaker_0"}
        """
        if not self.enabled:
            return {}

        try:
            # Extract unique speakers from segments
            unique_speakers = set()
            for segment in segments:
                if "speaker" in segment:
                    unique_speakers.add(segment["speaker"])

            logger.info(f"Identifying {len(unique_speakers)} speakers in {audio_path}")

            # Call the speaker recognition service
            async with aiohttp.ClientSession() as session:
                # Prepare the audio file for upload
                with open(audio_path, "rb") as audio_file:
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        "file", audio_file, filename=Path(audio_path).name, content_type="audio/wav"
                    )
                    # Get current diarization settings
                    from advanced_omi_backend.controllers.system_controller import _diarization_settings
                    
                    # Add all diarization parameters for the diarize-and-identify endpoint
                    form_data.add_field("min_duration", str(_diarization_settings.get("min_duration", 0.5)))
                    form_data.add_field("similarity_threshold", str(_diarization_settings.get("similarity_threshold", 0.15)))
                    form_data.add_field("collar", str(_diarization_settings.get("collar", 2.0)))
                    form_data.add_field("min_duration_off", str(_diarization_settings.get("min_duration_off", 1.5)))
                    if _diarization_settings.get("min_speakers"):
                        form_data.add_field("min_speakers", str(_diarization_settings["min_speakers"]))
                    if _diarization_settings.get("max_speakers"):
                        form_data.add_field("max_speakers", str(_diarization_settings["max_speakers"]))
                    form_data.add_field("identify_only_enrolled", "false")

                    # Make the request
                    async with session.post(
                        f"{self.service_url}/diarize-and-identify",
                        data=form_data,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as response:
                        if response.status != 200:
                            logger.warning(
                                f"Speaker recognition service returned status {response.status}: {await response.text()}"
                            )
                            return {}

                        result = await response.json()

                        # Process the response to create speaker mapping
                        speaker_mapping = self._process_diarization_result(result, segments)

                        if speaker_mapping:
                            logger.info(f"Speaker mapping created: {speaker_mapping}")
                        else:
                            logger.warning(
                                "No speaker mapping could be created from diarization result"
                            )

                        return speaker_mapping

        except aiohttp.ClientError as e:
            logger.warning(f"Failed to connect to speaker recognition service: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error during speaker identification: {e}")
            return {}

    def _process_diarization_result(
        self, diarization_result: Dict, original_segments: List[Dict]
    ) -> Dict[str, str]:
        """
        Process the diarization result to create a mapping from generic to identified speakers.

        Args:
            diarization_result: Response from the diarize-and-identify endpoint
            original_segments: Original transcript segments with generic speaker labels

        Returns:
            Dictionary mapping generic speaker labels to identified names
        """
        try:
            identified_segments = diarization_result.get("segments", [])

            # Create a mapping based on temporal overlap between segments
            speaker_mapping = {}
            unknown_counter = 0

            # Group diarization segments by their original speaker label
            diar_speakers = {}
            for seg in identified_segments:
                speaker_label = f"Speaker {seg.get('speaker', 0)}"
                if speaker_label not in diar_speakers:
                    diar_speakers[speaker_label] = []
                diar_speakers[speaker_label].append(seg)

            # Map each generic speaker to the most common identified speaker
            for generic_speaker in diar_speakers:
                segments_for_speaker = diar_speakers[generic_speaker]

                # Count identified names for this speaker
                name_counts = {}
                for seg in segments_for_speaker:
                    identified_name = seg.get("identified_as")
                    if identified_name and identified_name != "Unknown":
                        name_counts[identified_name] = name_counts.get(identified_name, 0) + 1

                # Assign the most common identified name, or unknown if none found
                if name_counts:
                    best_name = max(name_counts.items(), key=lambda x: x[1])[0]
                    speaker_mapping[generic_speaker] = best_name
                else:
                    speaker_mapping[generic_speaker] = f"unknown_speaker_{unknown_counter}"
                    unknown_counter += 1

            logger.info(f"🎤 Speaker mapping: {speaker_mapping}")
            return speaker_mapping

        except Exception as e:
            logger.error(f"🎤 Error processing diarization result: {e}")
            return {}

    async def get_enrolled_speakers(self, user_id: Optional[str] = None) -> Dict:
        """
        Get enrolled speakers from the speaker recognition service.

        Args:
            user_id: Optional user ID to filter speakers (for future user isolation)

        Returns:
            Dictionary containing speakers list and metadata
        """
        if not self.enabled:
            return {"speakers": []}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.service_url}/speakers",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        logger.warning(f"🎤 Failed to get enrolled speakers: status {response.status}")
                        return {"speakers": []}

                    result = await response.json()
                    speakers = result.get("speakers", [])
                    logger.info(f"🎤 Retrieved {len(speakers)} enrolled speakers")
                    return result

        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Failed to connect to speaker recognition service: {e}")
            return {"speakers": []}
        except Exception as e:
            logger.error(f"🎤 Error getting enrolled speakers: {e}")
            return {"speakers": []}

    async def check_if_enrolled_speaker_present(
        self,
        redis_client,
        client_id: str,
        session_id: str,
        user_id: str,
        transcription_results: List[dict]
    ) -> tuple[bool, dict]:
        """
        Check if any enrolled speakers are present in the transcription results.

        This extracts audio from Redis, runs speaker recognition, and checks if
        any identified speakers match the user's enrolled speakers.

        Args:
            redis_client: Redis client
            client_id: Client identifier
            session_id: Session identifier
            user_id: User ID
            transcription_results: List of transcription results from aggregator

        Returns:
            Tuple of (enrolled_present: bool, speaker_result: dict)
            - enrolled_present: True if enrolled speaker detected, False otherwise
            - speaker_result: Full speaker recognition result dict with segments
        """
        import tempfile
        import uuid
        from pathlib import Path
        from advanced_omi_backend.utils.audio_extraction import extract_audio_for_results
        from advanced_omi_backend.utils.audio_utils import write_pcm_to_wav

        logger.info(f"🎤 [SPEAKER CHECK] Starting speaker check for session {session_id}")
        logger.info(f"🎤 [SPEAKER CHECK] Client: {client_id}, User: {user_id}")
        logger.info(f"🎤 [SPEAKER CHECK] Transcription results count: {len(transcription_results)}")

        # Get enrolled speakers for this user
        logger.info(f"🎤 [SPEAKER CHECK] Fetching enrolled speakers for user {user_id}...")
        enrolled_result = await self.get_enrolled_speakers(user_id)
        enrolled_speakers = set(speaker["name"] for speaker in enrolled_result.get("speakers", []))

        logger.info(f"🎤 [SPEAKER CHECK] Enrolled speakers: {enrolled_speakers}")

        if not enrolled_speakers:
            logger.warning("🎤 [SPEAKER CHECK] No enrolled speakers found, allowing conversation")
            return (True, {})  # If no enrolled speakers, allow all conversations

        # Extract audio chunks
        logger.info(f"🎤 [SPEAKER CHECK] Extracting audio chunks from Redis...")
        audio_data = await extract_audio_for_results(
            redis_client=redis_client,
            client_id=client_id,
            session_id=session_id,
            transcription_results=transcription_results
        )

        if not audio_data:
            logger.warning("🎤 [SPEAKER CHECK] No audio data extracted, skipping speaker check")
            return (False, {})

        audio_size_kb = len(audio_data) / 1024
        audio_duration_sec = len(audio_data) / (16000 * 2)  # 16kHz, 16-bit
        logger.info(
            f"🎤 [SPEAKER CHECK] Extracted audio: {audio_size_kb:.1f} KB, ~{audio_duration_sec:.1f}s"
        )

        # Write to temporary WAV file
        temp_path = Path(tempfile.gettempdir()) / f"speech_check_{uuid.uuid4()}.wav"
        logger.info(f"🎤 [SPEAKER CHECK] Writing audio to temp file: {temp_path}")

        try:
            write_pcm_to_wav(audio_data, str(temp_path), sample_rate=16000, channels=1, sample_width=2)

            # Verify file was created
            if temp_path.exists():
                file_size = temp_path.stat().st_size
                logger.info(f"🎤 [SPEAKER CHECK] Temp WAV file created: {file_size} bytes")
            else:
                logger.error(f"🎤 [SPEAKER CHECK] ❌ Temp WAV file was not created!")
                return (False, {})

            # Run speaker recognition (diarize and identify)
            logger.info(f"🎤 [SPEAKER CHECK] Calling diarize_and_identify with audio file...")
            result = await self.diarize_and_identify(
                audio_path=str(temp_path),
                words=None,
                user_id=user_id
            )

            logger.info(f"🎤 [SPEAKER CHECK] Speaker recognition result: {result}")

            # Check if any identified speakers are enrolled
            identified_speakers = set()
            segments_count = len(result.get("segments", []))
            logger.info(f"🎤 [SPEAKER CHECK] Processing {segments_count} segments from speaker recognition")

            for idx, segment in enumerate(result.get("segments", [])):
                identified_name = segment.get("identified_as")
                speaker_label = segment.get("speaker", "unknown")
                segment_start = segment.get("start", 0)
                segment_end = segment.get("end", 0)

                logger.debug(
                    f"🎤 [SPEAKER CHECK] Segment {idx+1}/{segments_count}: "
                    f"speaker={speaker_label}, identified_as={identified_name}, "
                    f"time=[{segment_start:.2f}s - {segment_end:.2f}s]"
                )

                if identified_name and identified_name != "Unknown":
                    identified_speakers.add(identified_name)
                    logger.info(f"🎤 [SPEAKER CHECK] Found identified speaker: {identified_name}")

            logger.info(f"🎤 [SPEAKER CHECK] All identified speakers: {identified_speakers}")
            logger.info(f"🎤 [SPEAKER CHECK] Enrolled speakers: {enrolled_speakers}")

            matches = enrolled_speakers & identified_speakers

            if matches:
                logger.info(f"🎤 [SPEAKER CHECK] ✅ MATCH! Enrolled speaker(s) detected: {matches}")
                return (True, result)  # Return both boolean and speaker recognition results
            else:
                logger.info(
                    f"🎤 [SPEAKER CHECK] ❌ NO MATCH. "
                    f"Identified: {identified_speakers}, Enrolled: {enrolled_speakers}"
                )
                return (False, result)  # Return both boolean and speaker recognition results

        except Exception as e:
            logger.error(f"🎤 [SPEAKER CHECK] ❌ Speaker recognition check failed: {e}", exc_info=True)
            return (False, {})  # Fail closed - don't create conversation on error

        finally:
            # Clean up temp file
            try:
                if temp_path.exists():
                    temp_path.unlink()
                    logger.debug(f"🎤 [SPEAKER CHECK] Cleaned up temp file: {temp_path}")
            except Exception as cleanup_error:
                logger.warning(f"🎤 [SPEAKER CHECK] Failed to remove temp file {temp_path}: {cleanup_error}")

    async def health_check(self) -> bool:
        """
        Check if the speaker recognition service is healthy and responding.

        Returns:
            True if service is healthy, False otherwise
        """
        if not self.enabled:
            return False

        try:
            logger.debug(f"Performing health check on speaker service: {self.service_url}")

            async with aiohttp.ClientSession() as session:
                # Use the /health endpoint if available, otherwise try a simple endpoint
                health_endpoints = ["/health", "/speakers"]

                for endpoint in health_endpoints:
                    try:
                        async with session.get(
                            f"{self.service_url}{endpoint}",
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as response:
                            if response.status == 200:
                                logger.debug(f"Speaker service health check passed via {endpoint}")
                                return True
                            else:
                                logger.debug(f"Health check endpoint {endpoint} returned {response.status}")
                    except Exception as endpoint_error:
                        logger.debug(f"Health check failed for {endpoint}: {endpoint_error}")
                        continue

                logger.warning("All health check endpoints failed")
                return False

        except Exception as e:
            logger.error(f"Error during speaker service health check: {e}")
            return False
