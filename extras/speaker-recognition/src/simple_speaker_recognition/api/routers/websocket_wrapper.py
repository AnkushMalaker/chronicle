"""WebSocket wrapper for Deepgram with speaker change detection using Pyannote VAD."""

import asyncio
import json
import logging
import os
import wave
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode

import numpy as np
import torch
import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pyannote.audio import Model
from pyannote.audio.pipelines import VoiceActivityDetection
from simple_speaker_recognition.api.core.utils import (
    safe_format_confidence,
    validate_confidence,
)
from simple_speaker_recognition.core.models import SpeakerStatus
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB

router = APIRouter()
log = logging.getLogger("websocket_wrapper")


# Dependency functions
async def get_db():
    """Get speaker database dependency."""
    from .. import service

    return await service.get_db()


def get_audio_backend():
    """Get audio backend."""
    from .. import service

    return service.audio_backend


def get_speaker_db():
    """Get speaker database."""
    from .. import service

    return service.speaker_db


def get_auth():
    """Get auth settings."""
    from .. import service

    return service.auth


class AudioSegmentBuffer:
    """Manages a rolling buffer of audio segments for VAD processing."""

    def __init__(self, max_duration_seconds: float = 20.0, sample_rate: int = 16000):
        self.max_duration = max_duration_seconds
        self.sample_rate = sample_rate
        self.max_samples = int(max_duration_seconds * sample_rate)
        self.buffer = deque(maxlen=self.max_samples)
        self.timestamp_offset = 0.0  # Track absolute time offset

    def add_audio(self, audio_data: np.ndarray):
        """Add audio samples to the buffer."""
        # Convert to float32 if needed
        if audio_data.dtype == np.int16:
            audio_data = audio_data.astype(np.float32) / 32768.0

        # Add to buffer
        self.buffer.extend(audio_data)

    def get_audio_tensor(
        self, start_time: float = 0.0, duration: float = None
    ) -> torch.Tensor:
        """Get audio as tensor for a specific time range."""
        if duration is None:
            # Get all buffered audio
            audio_array = np.array(self.buffer, dtype=np.float32)
        else:
            # Get specific segment
            start_sample = int(start_time * self.sample_rate)
            end_sample = int((start_time + duration) * self.sample_rate)
            audio_array = np.array(
                list(self.buffer)[start_sample:end_sample], dtype=np.float32
            )

        # Convert to tensor with shape (1, num_samples) for pyannote
        return torch.from_numpy(audio_array).unsqueeze(0)

    def get_duration(self) -> float:
        """Get current buffer duration in seconds."""
        return len(self.buffer) / self.sample_rate

    def clear(self):
        """Clear the buffer."""
        self.buffer.clear()
        self.timestamp_offset = 0.0


class SpeakerChangeDetector:
    """Detects speaker changes using Pyannote VAD."""

    def __init__(self, hf_token: str = None, device: str = "cpu"):
        self.device = torch.device(device)
        self.hf_token = hf_token or os.getenv("HF_TOKEN")

        # Initialize VAD pipeline
        self.vad_model = None
        self.vad_pipeline = None
        self.initialize_vad()

        # Track speech segments
        self.last_speech_end = 0.0
        self.min_silence_duration = 1.5  # Minimum silence for utterance boundary
        self.current_segment_start = None
        self.current_segment_audio = []

    def initialize_vad(self):
        """Initialize Pyannote VAD pipeline."""
        try:
            # Load segmentation model for VAD
            log.info("Loading Pyannote VAD model...")
            self.vad_model = Model.from_pretrained(
                "pyannote/segmentation-3.0", token=self.hf_token
            )

            # Create VAD pipeline
            self.vad_pipeline = VoiceActivityDetection(segmentation=self.vad_model)

            # Configure VAD parameters for real-time processing
            # Note: Some parameters may not be available in all versions
            try:
                hyperparameters = {
                    "onset": 0.5,  # Lower = more sensitive to speech start
                    "offset": 0.5,  # Lower = more sensitive to speech end
                    "min_duration_on": 0.1,  # Minimum speech duration
                    "min_duration_off": 0.5,  # Minimum silence duration
                }
                self.vad_pipeline.instantiate(hyperparameters)
            except Exception as e:
                log.warning(f"Failed to set custom VAD hyperparameters: {e}")
                try:
                    # Try with empty params dict for newer pyannote versions
                    self.vad_pipeline.instantiate({})
                except Exception as e2:
                    log.error(
                        f"Failed to instantiate VAD pipeline with empty params: {e2}"
                    )
                    # Set to None to use fallback VAD
                    self.vad_pipeline = None

            log.info("VAD pipeline initialized successfully")

        except Exception as e:
            log.error(f"Failed to initialize VAD: {e}")
            # Fall back to simple energy-based VAD if Pyannote fails
            self.vad_pipeline = None

    def detect_speech_segments(
        self, audio_tensor: torch.Tensor, offset: float = 0.0
    ) -> List[Tuple[float, float]]:
        """Detect speech segments in audio using VAD."""
        if self.vad_pipeline is None:
            # Fallback to simple energy-based detection
            return self._simple_vad(audio_tensor, offset)

        try:
            # Run VAD pipeline
            speech_segments = self.vad_pipeline(
                {"waveform": audio_tensor, "sample_rate": 16000}
            )

            # Convert to list of (start, end) tuples
            segments = []
            for segment in speech_segments:
                segments.append((segment.start + offset, segment.end + offset))

            return segments

        except Exception as e:
            log.warning(f"VAD failed, using fallback: {e}")
            return self._simple_vad(audio_tensor, offset)

    def _simple_vad(
        self, audio_tensor: torch.Tensor, offset: float = 0.0
    ) -> List[Tuple[float, float]]:
        """Simple energy-based VAD as fallback."""
        audio = audio_tensor.squeeze().numpy()

        # Calculate energy in frames
        frame_size = 480  # 30ms at 16kHz
        hop_size = 160  # 10ms at 16kHz

        segments = []
        is_speech = False
        segment_start = None

        for i in range(0, len(audio) - frame_size, hop_size):
            frame = audio[i : i + frame_size]
            energy = np.mean(frame**2)

            # Simple threshold (adjust as needed)
            threshold = 0.001

            if energy > threshold and not is_speech:
                # Speech started
                is_speech = True
                segment_start = (i / 16000) + offset
            elif energy <= threshold and is_speech:
                # Speech ended
                is_speech = False
                if segment_start is not None:
                    segment_end = (i / 16000) + offset
                    if segment_end - segment_start > 0.1:  # Min duration
                        segments.append((segment_start, segment_end))
                segment_start = None

        # Handle ongoing speech at end
        if is_speech and segment_start is not None:
            segments.append((segment_start, (len(audio) / 16000) + offset))

        return segments

    def detect_utterance_boundary(
        self, audio_buffer: AudioSegmentBuffer, current_time: float
    ) -> Optional[Tuple[float, float]]:
        """Detect if an utterance boundary has occurred."""
        # Get recent audio (last 5 seconds)
        lookback_duration = min(5.0, audio_buffer.get_duration())
        if lookback_duration < 0.5:
            return None

        # Get audio tensor for VAD
        audio_tensor = audio_buffer.get_audio_tensor(
            start_time=max(0, audio_buffer.get_duration() - lookback_duration),
            duration=lookback_duration,
        )

        # Detect speech segments
        time_offset = current_time - lookback_duration
        segments = self.detect_speech_segments(audio_tensor, time_offset)

        if not segments:
            # No speech detected
            if self.current_segment_start is not None:
                # We were in a speech segment, now it's ended
                segment_end = self.last_speech_end
                segment_start = self.current_segment_start
                self.current_segment_start = None

                if segment_end - segment_start > 0.5:  # Min utterance duration
                    return (segment_start, segment_end)
            return None

        # Check for gaps between segments (potential boundaries)
        for i in range(len(segments) - 1):
            gap_duration = segments[i + 1][0] - segments[i][1]
            if gap_duration >= self.min_silence_duration:
                # Found a boundary
                if self.current_segment_start is not None:
                    segment_start = self.current_segment_start
                    segment_end = segments[i][1]
                    self.current_segment_start = segments[i + 1][0]
                    self.last_speech_end = segment_end

                    if segment_end - segment_start > 0.5:  # Min utterance duration
                        return (segment_start, segment_end)

        # Update tracking
        if segments:
            self.last_speech_end = segments[-1][1]
            if self.current_segment_start is None:
                self.current_segment_start = segments[0][0]

        return None


class DeepgramWebSocketProxy:
    """Proxies audio to Deepgram and handles transcription."""

    def __init__(
        self,
        api_key: str,
        deepgram_params: Dict[str, str] = None,
        on_transcript_callback=None,
        on_raw_deepgram_callback=None,
    ):
        self.api_key = api_key
        self.deepgram_params = deepgram_params or {}
        self.ws_connection = None
        self.on_transcript = on_transcript_callback
        self.on_raw_deepgram = on_raw_deepgram_callback
        self.is_connected = False

    async def connect(self):
        """Connect to Deepgram WebSocket API."""
        try:
            url = "wss://api.deepgram.com/v1/listen"

            # Use provided parameters or sensible defaults
            default_params = {
                "model": "nova-3",
                "language": "multi",
                "encoding": "linear16",
                "sample_rate": "16000",
                "channels": "1",
                "punctuate": "true",
                "smart_format": "true",
                "interim_results": "true",
            }

            # Merge defaults with provided parameters (provided parameters take precedence)
            params = {**default_params, **self.deepgram_params}

            # Build URL with parameters
            param_str = urlencode(params)
            full_url = f"{url}?{param_str}"

            log.info(f"Connecting to Deepgram: {url}")
            log.debug(f"Parameters: {param_str}")

            # Connect with authorization using additional_headers (websockets 15.0+)
            self.ws_connection = await websockets.connect(
                full_url, additional_headers={"Authorization": f"Token {self.api_key}"}
            )

            self.is_connected = True
            log.info("Connected to Deepgram WebSocket API")

            # Start listening for responses
            asyncio.create_task(self._listen_for_responses())

        except Exception as e:
            log.error(f"Failed to connect to Deepgram: {e}")
            self.is_connected = False
            raise

    async def _listen_for_responses(self):
        """Listen for transcription responses from Deepgram."""
        try:
            async for message in self.ws_connection:
                try:
                    data = json.loads(message)

                    # Forward ALL Deepgram messages raw
                    if self.on_raw_deepgram:
                        await self.on_raw_deepgram(data)

                    # Extract transcript for our VAD processing (keep existing logic)
                    if "channel" in data:
                        alternatives = data.get("channel", {}).get("alternatives", [])
                        if alternatives and alternatives[0].get("transcript"):
                            transcript = alternatives[0]["transcript"]
                            is_final = data.get("is_final", False)

                            if self.on_transcript:
                                await self.on_transcript(
                                    {
                                        "transcript": transcript,
                                        "is_final": is_final,
                                        "words": alternatives[0].get("words", []),
                                    }
                                )

                except json.JSONDecodeError:
                    log.warning("Received non-JSON message from Deepgram")

        except websockets.ConnectionClosed:
            log.info("Deepgram connection closed")
            self.is_connected = False
        except Exception as e:
            log.error(f"Error in Deepgram listener: {e}")
            self.is_connected = False

    async def send_audio(self, audio_data: bytes):
        """Send audio data to Deepgram."""
        if self.is_connected and self.ws_connection:
            try:
                await self.ws_connection.send(audio_data)
            except Exception as e:
                log.error(f"Failed to send audio to Deepgram: {e}")
                self.is_connected = False

    async def disconnect(self):
        """Disconnect from Deepgram."""
        if self.ws_connection:
            await self.ws_connection.close()
            self.is_connected = False


@router.websocket("/ws/streaming-with-scd")
async def websocket_streaming_with_scd(
    websocket: WebSocket,
    user_id: Optional[int] = Query(
        default=None, description="User ID for speaker identification"
    ),
    confidence_threshold: float = Query(
        default=0.15, description="Speaker identification confidence threshold"
    ),
    utterance_end_ms: int = Query(
        default=1000,
        description="How long to wait after silence before completing an utterance (ms)",
    ),
    endpointing_ms: int = Query(
        default=300, description="Silence detection timeout for interim results (ms)"
    ),
    interim_results: bool = Query(
        default=True, description="Enable interim transcription results"
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    WebSocket endpoint for real-time transcription with speaker change detection.

    Combines:
    - Deepgram streaming transcription
    - Pyannote VAD-based utterance boundary detection
    - Speaker identification on detected segments

    Protocol:
    - Client sends: Raw PCM audio chunks (16kHz, mono, 16-bit)
    - Server sends: JSON events with utterance boundaries and speaker info
    """

    # Get dependencies BEFORE accepting WebSocket to avoid timeout issues
    auth = get_auth()
    audio_backend = get_audio_backend()
    speaker_db = get_speaker_db()

    # Extract Deepgram API key from WebSocket subprotocols (like Deepgram does)
    api_key = None
    if hasattr(websocket, "scope") and "subprotocols" in websocket.scope:
        subprotocols = websocket.scope["subprotocols"]
        log.info(f"DEBUG: Received subprotocols: {subprotocols}")
        # Look for 'token' protocol followed by API key
        if len(subprotocols) >= 2 and subprotocols[0] == "token":
            api_key = subprotocols[1]
            log.info("Extracted Deepgram API key from WebSocket subprotocols")
    else:
        log.info("DEBUG: No subprotocols in WebSocket scope")

    # Fallback to server default API key
    api_key = api_key or auth.deepgram_api_key
    if not api_key:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "message": "Deepgram API key required"}
        )
        await websocket.close()
        return

    # Check HF token before initializing VAD
    if not auth.hf_token:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "message": "Hugging Face token not configured on server"}
        )
        await websocket.close()
        return

    # Initialize heavy components BEFORE accepting WebSocket
    log.info("Initializing components before WebSocket accept...")

    # Initialize audio buffer
    audio_buffer = AudioSegmentBuffer(max_duration_seconds=20.0)

    # Initialize SCD (this loads VAD model - takes ~1 second)
    log.info("Loading Speaker Change Detector...")
    scd = SpeakerChangeDetector(hf_token=auth.hf_token)

    # Setup and connect Deepgram proxy (this connects to Deepgram - takes ~1 second)
    log.info("Setting up Deepgram connection...")
    # Track transcription state
    current_transcript = []

    # Setup Deepgram proxy
    async def on_deepgram_transcript(data: Dict[str, Any]):
        """Handle transcription from Deepgram."""
        nonlocal current_transcript

        if data["is_final"]:
            current_transcript.append(data["transcript"])
            log.debug(f"Final transcript: {data['transcript']}")

    async def on_raw_deepgram(data: Dict[str, Any]):
        """Forward all raw Deepgram messages to client."""
        try:
            await websocket.send_json(
                {
                    "type": "raw_deepgram",
                    "data": data,
                    "timestamp": asyncio.get_event_loop().time(),
                }
            )
        except Exception as e:
            log.warning(f"Failed to send raw Deepgram event: {e}")

    deepgram_proxy = DeepgramWebSocketProxy(
        api_key=api_key,
        on_transcript_callback=on_deepgram_transcript,
        on_raw_deepgram_callback=on_raw_deepgram,
    )

    # Connect to Deepgram before accepting WebSocket
    try:
        await deepgram_proxy.connect()
        log.info("✅ All components initialized successfully")
    except Exception as e:
        log.error(f"Failed to initialize components: {e}")
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "message": f"Failed to initialize: {str(e)}"}
        )
        await websocket.close()
        return

    # NOW accept the WebSocket - everything is ready!
    log.info(f"DEBUG: About to accept WebSocket for user_id: {user_id}")
    # Accept with subprotocol negotiation (required for browser compatibility)
    await websocket.accept(subprotocol="token")
    log.info(
        f"✅ WebSocket connection accepted for user_id: {user_id} - all components ready"
    )

    # Track stream start time
    stream_start_time = asyncio.get_event_loop().time()

    # Create debug WAV file
    debug_dir = "/app/debug"
    os.makedirs(debug_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_filename = f"{debug_dir}/debug_session_{timestamp}_user{user_id}.wav"

    # Initialize WAV file (16-bit PCM, 16kHz, mono)
    wav_file = wave.open(wav_filename, "wb")
    wav_file.setnchannels(1)  # mono
    wav_file.setsampwidth(2)  # 16-bit
    wav_file.setframerate(16000)  # 16kHz
    log.info(f"🎙️ Created debug WAV file: {wav_filename}")

    try:
        # Send initial ready event (components already initialized and connected)
        log.info("DEBUG: Sending ready message to client...")
        try:
            await websocket.send_json(
                {"type": "ready", "message": "WebSocket ready for audio streaming"}
            )
            log.info("DEBUG: Ready message sent successfully")
        except Exception as e:
            log.error(f"DEBUG: Failed to send ready message: {e}")
            raise

        # Main processing loop
        log.info("Starting main WebSocket processing loop")
        while True:
            try:
                log.info("DEBUG: Waiting for audio data from client...")
                # Receive audio data from client
                data = await websocket.receive_bytes()
                log.info(f"DEBUG: Received {len(data)} bytes of audio data")

                # Write audio data to debug WAV file
                wav_file.writeframes(data)

                # Get current time
                current_time = asyncio.get_event_loop().time() - stream_start_time

                # Convert bytes to numpy array (assuming 16-bit PCM)
                audio_array = np.frombuffer(data, dtype=np.int16)

                # Add to buffer
                audio_buffer.add_audio(audio_array)

                # Forward to Deepgram
                await deepgram_proxy.send_audio(data)

                # Check for utterance boundary
                boundary = scd.detect_utterance_boundary(audio_buffer, current_time)

                if boundary:
                    start_time, end_time = boundary

                    # Combine transcript segments for this utterance
                    utterance_text = " ".join(current_transcript).strip()
                    current_transcript = []

                    log.info(
                        f"Utterance detected: {start_time:.2f}s - {end_time:.2f}s, Text: {utterance_text[:50]}..."
                    )

                    # Prepare response event
                    event = {
                        "type": "utterance_boundary",
                        "timestamp": current_time,
                        "audio_segment": {
                            "start": start_time,
                            "end": end_time,
                            "duration": end_time - start_time,
                        },
                        "transcript": utterance_text,
                        "speaker_identification": None,  # Will be populated if user_id provided
                    }

                    # Perform speaker identification if user_id provided
                    if user_id and utterance_text:
                        try:
                            # Extract audio segment for identification
                            segment_duration = end_time - start_time
                            segment_audio = audio_buffer.get_audio_tensor(
                                start_time=max(
                                    0,
                                    audio_buffer.get_duration()
                                    - (current_time - start_time),
                                ),
                                duration=segment_duration,
                            )

                            # Get embedding
                            emb = await audio_backend.async_embed(
                                segment_audio.unsqueeze(0)
                            )

                            # Identify speaker
                            found, speaker_info, confidence = await speaker_db.identify(
                                emb, user_id=user_id
                            )
                            confidence = validate_confidence(
                                confidence, "websocket_scd"
                            )

                            if found and confidence >= confidence_threshold:
                                event["speaker_identification"] = {
                                    "speaker_id": speaker_info["id"],
                                    "speaker_name": speaker_info["name"],
                                    "confidence": float(confidence),
                                    "status": SpeakerStatus.IDENTIFIED.value,
                                }
                                log.info(
                                    f"Speaker identified: {speaker_info['name']} (confidence: {confidence:.3f})"
                                )
                            else:
                                event["speaker_identification"] = {
                                    "speaker_id": None,
                                    "speaker_name": None,
                                    "confidence": (
                                        float(confidence) if confidence else 0.0
                                    ),
                                    "status": SpeakerStatus.UNKNOWN.value,
                                }

                        except Exception as e:
                            log.error(f"Speaker identification failed: {e}")
                            event["speaker_identification"] = {
                                "status": SpeakerStatus.ERROR.value,
                                "error": str(e),
                            }

                    # Send event to client
                    await websocket.send_json(event)

            except WebSocketDisconnect as e:
                log.info(f"Client disconnected: code={e.code}, reason={e.reason}")
                break
            except Exception as e:
                log.error(f"Error processing audio: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})

    except Exception as e:
        log.error(f"WebSocket error: {e}")
        log.error(f"Error type: {type(e).__name__}")
        log.error(f"Error traceback:", exc_info=True)

        # Try to send error to client if connection is still open
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"WebSocket processing error: {str(e)}",
                    }
                )
        except Exception as send_error:
            log.warning(f"Could not send error to client: {send_error}")

    finally:
        # Cleanup
        await deepgram_proxy.disconnect()

        # Close and finalize debug WAV file
        try:
            wav_file.close()
            log.info(f"🎙️ Closed debug WAV file: {wav_filename}")
        except Exception as wav_error:
            log.warning(f"Failed to close WAV file: {wav_error}")

        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close()
        except Exception as e:
            log.warning(f"Error closing WebSocket (already closed?): {e}")
        log.info("WebSocket connection closed")


@router.get("/ws/streaming-with-scd/info")
async def get_streaming_info():
    """Get information about the streaming WebSocket endpoint."""
    return {
        "endpoint": "/ws/streaming-with-scd",
        "description": "WebSocket endpoint for real-time transcription with speaker change detection",
        "protocol": {
            "input": "Raw PCM audio (16kHz, mono, 16-bit)",
            "output": "JSON events with utterance boundaries and speaker identification",
        },
        "parameters": {
            "user_id": "User ID for speaker identification (optional)",
            "confidence_threshold": "Minimum confidence for speaker identification (default: 0.15)",
            "deepgram_api_key": "Deepgram API key (optional, uses server default if not provided)",
        },
        "events": {
            "ready": "Sent when WebSocket is ready to receive audio",
            "utterance_boundary": "Sent when an utterance boundary is detected",
            "error": "Sent when an error occurs",
        },
    }


@router.websocket("/v1/ws_listen")
async def deepgram_proxy_websocket(
    websocket: WebSocket,
    user_id: Optional[int] = Query(
        default=None, description="User ID for speaker identification (enhancement)"
    ),
    confidence_threshold: float = Query(
        default=0.15,
        description="Speaker identification confidence threshold (enhancement)",
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """
    WebSocket streaming endpoint with Deepgram proxy and speaker identification.

    This endpoint:
    - Acts as a transparent proxy to Deepgram WebSocket API
    - Forwards all Deepgram responses as raw_deepgram events
    - Adds speaker identification when user_id is provided
    - Supports WebSocket subprotocol authentication (Deepgram style)

    Enhancement Parameters (not forwarded to Deepgram):
    - user_id: Enable speaker identification for enrolled speakers
    - confidence_threshold: Minimum confidence for speaker matching

    All other parameters are forwarded to Deepgram unchanged.

    Note: For file uploads, use POST /v1/listen endpoint instead.
    """

    # Get dependencies BEFORE accepting WebSocket
    auth = get_auth()
    audio_backend = get_audio_backend()
    speaker_db = get_speaker_db()

    # Extract ALL query parameters from WebSocket scope
    query_string = websocket.scope.get("query_string", b"").decode("utf-8")
    parsed_params = parse_qs(query_string)
    # Convert from list values to single values (FastAPI style)
    all_params = {k: v[0] if v else "" for k, v in parsed_params.items()}
    log.info(f"Received parameters: {list(all_params.keys())}")

    # Extract our enhancement parameters
    enhancement_params = {
        "user_id": all_params.pop("user_id", None),
        "confidence_threshold": all_params.pop("confidence_threshold", "0.15"),
    }

    # Convert confidence threshold to float
    try:
        confidence_threshold = float(enhancement_params["confidence_threshold"])
    except (ValueError, TypeError):
        confidence_threshold = 0.15

    # Parse user_id
    if enhancement_params["user_id"]:
        try:
            user_id = int(enhancement_params["user_id"])
        except (ValueError, TypeError):
            user_id = None
    else:
        user_id = None

    log.info(
        f"Enhancement params - user_id: {user_id}, confidence_threshold: {confidence_threshold}"
    )
    log.info(f"Deepgram params: {list(all_params.keys())}")

    # Extract Deepgram API key from WebSocket subprotocols (same as existing endpoint)
    api_key = None
    if hasattr(websocket, "scope") and "subprotocols" in websocket.scope:
        subprotocols = websocket.scope["subprotocols"]
        if len(subprotocols) >= 2 and subprotocols[0] == "token":
            api_key = subprotocols[1]
            log.info("Extracted Deepgram API key from WebSocket subprotocols")

    # Fallback to server default API key
    api_key = api_key or auth.deepgram_api_key
    if not api_key:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "message": "Deepgram API key required"}
        )
        await websocket.close()
        return

    # Initialize components if speaker identification is enabled
    audio_buffer = None
    scd = None
    if user_id:
        # Check HF token for VAD
        if not auth.hf_token:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Hugging Face token not configured - required for speaker identification",
                }
            )
            await websocket.close()
            return

        log.info("Initializing speaker identification components...")
        audio_buffer = AudioSegmentBuffer(max_duration_seconds=20.0)
        scd = SpeakerChangeDetector(hf_token=auth.hf_token)

    # Track transcription state for speaker identification
    current_transcript = []

    # Setup Deepgram proxy with dynamic parameters
    async def on_deepgram_transcript(data: Dict[str, Any]):
        """Handle transcription from Deepgram (for speaker identification)."""
        nonlocal current_transcript

        if data["is_final"]:
            current_transcript.append(data["transcript"])
            log.debug(f"Final transcript: {data['transcript']}")

    async def on_raw_deepgram(data: Dict[str, Any]):
        """Forward all raw Deepgram messages to client."""
        try:
            await websocket.send_json(
                {
                    "type": "raw_deepgram",
                    "data": data,
                    "timestamp": asyncio.get_event_loop().time(),
                }
            )
        except Exception as e:
            log.warning(f"Failed to send raw Deepgram event: {e}")

    deepgram_proxy = DeepgramWebSocketProxy(
        api_key=api_key,
        deepgram_params=all_params,  # Forward all remaining parameters to Deepgram
        on_transcript_callback=on_deepgram_transcript,
        on_raw_deepgram_callback=on_raw_deepgram,
    )

    # Connect to Deepgram before accepting WebSocket
    try:
        await deepgram_proxy.connect()
        log.info("✅ Deepgram proxy connected successfully")
    except Exception as e:
        log.error(f"Failed to connect to Deepgram: {e}")
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "message": f"Failed to connect to Deepgram: {str(e)}"}
        )
        await websocket.close()
        return

    # NOW accept the WebSocket - everything is ready!
    await websocket.accept(subprotocol="token")
    log.info(f"✅ WebSocket connection accepted - Deepgram proxy ready")

    # Track stream start time for speaker identification
    stream_start_time = asyncio.get_event_loop().time()

    # Create debug WAV file if speaker identification is enabled
    wav_file = None
    if user_id:
        debug_dir = "/app/debug"
        os.makedirs(debug_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_filename = f"{debug_dir}/debug_session_{timestamp}_user{user_id}.wav"

        wav_file = wave.open(wav_filename, "wb")
        wav_file.setnchannels(1)  # mono
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(16000)  # 16kHz
        log.info(f"🎙️ Created debug WAV file: {wav_filename}")

    try:
        # Send ready event
        await websocket.send_json(
            {
                "type": "ready",
                "message": "Universal Deepgram proxy ready",
                "features": {
                    "raw_deepgram_forwarding": True,
                    "speaker_identification": user_id is not None,
                    "debug_recording": user_id is not None,
                },
            }
        )

        # Main processing loop
        while True:
            try:
                # Receive audio data from client
                data = await websocket.receive_bytes()

                # Write to debug WAV file if enabled
                if wav_file:
                    wav_file.writeframes(data)

                # Forward to Deepgram
                await deepgram_proxy.send_audio(data)

                # Speaker identification processing (if enabled)
                if user_id and audio_buffer and scd:
                    current_time = asyncio.get_event_loop().time() - stream_start_time

                    # Convert bytes to numpy array
                    audio_array = np.frombuffer(data, dtype=np.int16)
                    audio_buffer.add_audio(audio_array)

                    # Check for utterance boundary
                    boundary = scd.detect_utterance_boundary(audio_buffer, current_time)

                    if boundary:
                        start_time, end_time = boundary
                        utterance_text = " ".join(current_transcript).strip()
                        current_transcript = []

                        log.info(
                            f"Utterance detected: {start_time:.2f}s - {end_time:.2f}s, Text: {utterance_text[:50]}..."
                        )

                        # Prepare speaker identification event
                        event = {
                            "type": "speaker_identified",
                            "timestamp": current_time,
                            "audio_segment": {
                                "start": start_time,
                                "end": end_time,
                                "duration": end_time - start_time,
                            },
                            "transcript": utterance_text,
                            "speaker_identification": None,
                        }

                        # Perform speaker identification
                        if utterance_text:
                            try:
                                # Extract audio segment
                                segment_duration = end_time - start_time
                                segment_audio = audio_buffer.get_audio_tensor(
                                    start_time=max(
                                        0,
                                        audio_buffer.get_duration()
                                        - (current_time - start_time),
                                    ),
                                    duration=segment_duration,
                                )

                                # Get embedding and identify speaker
                                emb = await audio_backend.async_embed(
                                    segment_audio.unsqueeze(0)
                                )
                                found, speaker_info, confidence = (
                                    await speaker_db.identify(emb, user_id=user_id)
                                )
                                confidence = validate_confidence(
                                    confidence, "deepgram_proxy"
                                )

                                if found and confidence >= confidence_threshold:
                                    event["speaker_identification"] = {
                                        "speaker_id": speaker_info["id"],
                                        "speaker_name": speaker_info["name"],
                                        "confidence": float(confidence),
                                        "status": SpeakerStatus.IDENTIFIED.value,
                                    }
                                    log.info(
                                        f"Speaker identified: {speaker_info['name']} (confidence: {confidence:.3f})"
                                    )
                                else:
                                    event["speaker_identification"] = {
                                        "speaker_id": None,
                                        "speaker_name": None,
                                        "confidence": (
                                            float(confidence) if confidence else 0.0
                                        ),
                                        "status": SpeakerStatus.UNKNOWN.value,
                                    }

                            except Exception as e:
                                log.error(f"Speaker identification failed: {e}")
                                event["speaker_identification"] = {
                                    "status": SpeakerStatus.ERROR.value,
                                    "error": str(e),
                                }

                        # Send speaker identification event
                        await websocket.send_json(event)

            except WebSocketDisconnect as e:
                log.info(f"Client disconnected: code={e.code}")
                break
            except Exception as e:
                log.error(f"Error processing audio: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})

    except Exception as e:
        log.error(f"WebSocket error: {e}")
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.send_json(
                    {"type": "error", "message": f"Proxy error: {str(e)}"}
                )
        except Exception as send_error:
            log.warning(f"Could not send error to client: {send_error}")

    finally:
        # Cleanup
        await deepgram_proxy.disconnect()

        # Close debug WAV file
        if wav_file:
            try:
                wav_file.close()
                log.info(f"🎙️ Closed debug WAV file")
            except Exception as wav_error:
                log.warning(f"Failed to close WAV file: {wav_error}")

        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close()
        except Exception as e:
            log.warning(f"Error closing WebSocket: {e}")
        log.info("Universal Deepgram proxy connection closed")


@router.get("/v1/listen/info")
async def get_deepgram_api_info():
    """Get information about the Deepgram-compatible APIs with separate POST and WebSocket endpoints."""
    return {
        "endpoints": {"file_upload": "/v1/listen", "websocket": "/v1/ws_listen"},
        "description": "Complete Deepgram API drop-in replacement with separate endpoints",
        "compatibility": "Full Deepgram API compatibility for file upload (POST /v1/listen) and streaming (WSS /v1/ws_listen)",
        "protocols": {
            "POST": {
                "description": "File upload transcription (multipart/form-data)",
                "use_case": "Pre-recorded audio files",
                "input": "Audio files (wav, mp3, flac, etc.)",
                "output": "Deepgram transcription response with optional speaker enhancement",
                "authentication": "Authorization: Token YOUR_DEEPGRAM_API_KEY",
                "example": "curl -X POST -H 'Authorization: Token API_KEY' -F 'file=@audio.wav' /v1/listen",
            },
            "WebSocket": {
                "description": "Real-time streaming transcription",
                "use_case": "Live audio streaming",
                "input": "Raw audio data (binary WebSocket messages)",
                "output": "All Deepgram events + optional speaker identification",
                "authentication": [
                    "Authorization: Token YOUR_DEEPGRAM_API_KEY",
                    "WebSocket subprotocols: ['token', 'YOUR_DEEPGRAM_API_KEY']",
                ],
                "example": "const ws = new WebSocket('wss://host/v1/ws_listen?model=nova-3', ['token', 'API_KEY'])",
            },
        },
        "parameters": {
            "deepgram": "All 25+ Deepgram parameters supported (model, language, encoding, etc.)",
            "enhancements": {
                "user_id": "User ID for speaker identification (optional)",
                "confidence_threshold": "Speaker matching threshold (default: 0.15)",
            },
        },
        "websocket_events": {
            "ready": "Proxy initialization complete",
            "raw_deepgram": "All Deepgram WebSocket events forwarded transparently",
            "speaker_identified": "Speaker identification results (when user_id provided)",
            "error": "Processing errors",
        },
        "features": [
            "True Deepgram API drop-in replacement",
            "Both POST file upload and WebSocket streaming on same path",
            "Complete parameter compatibility",
            "Transparent event forwarding (WebSocket)",
            "Optional speaker identification enhancement",
            "Debug WAV recording (WebSocket with user_id)",
            "Dynamic parameter handling",
        ],
    }
