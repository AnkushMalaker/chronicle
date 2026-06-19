"""Consumes the wakeword:detections Redis stream and dispatches plugin events.

The standalone wakeword-service publishes one message per captured wake-word
turn to ``wakeword:detections``. This dispatcher reads that stream with a
consumer group, resolves the current conversation id for the session, and calls
``PluginRouter.dispatch_event(PluginEvent.WAKE_WORD_DETECTED, ...)`` — the same
router the text keyword path uses, so both reach the Hermes agent identically.
"""

import asyncio
import base64
import io
import json
import logging
import os
import time
import wave

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from advanced_omi_backend.config import get_wakeword_command_source
from advanced_omi_backend.heartbeat import beat
from advanced_omi_backend.plugins.router import (
    PluginRouter,
    extract_command_around_keyword,
)
from advanced_omi_backend.services.audio_stream.aggregator import (
    TranscriptionResultsAggregator,
)
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.services.wakeword.executor import (
    execute_voice_command,
    get_current_conversation_id,
    publish_sse,
)

logger = logging.getLogger(__name__)

DETECTIONS_STREAM = "wakeword:detections"
GROUP_NAME = "wakeword-dispatch"

# Spoken wake words to strip off the front of a streaming-derived command (the
# streaming transcript includes the wake word; the batch capture does not).
# Shared default with the follow-up handler (followup.py). Longest first so
# "hey hermes" is removed before the bare "hermes".
_SPOKEN_WAKE_WORDS = sorted(
    (
        w.strip()
        for w in os.getenv("FOLLOWUP_WAKE_WORDS", "hey hermes,hermes").split(",")
        if w.strip()
    ),
    key=len,
    reverse=True,
)

# How long to wait for the live streaming transcript of the command window to
# land when we read it (the final streaming result can arrive a beat after the
# wake-word turn-end fires).
_STREAMING_POLL_SECS = float(os.getenv("WAKEWORD_STREAMING_POLL_SECS", "3.0"))
_STREAMING_POLL_INTERVAL = 0.3
# Slack around the capture window when matching streaming results by wall clock.
_STREAMING_WINDOW_MARGIN_SECS = 2.0


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw int16 mono PCM in a WAV container.

    The batch provider auto-detects WAV via the RIFF header and sets the correct
    content-type; sending raw PCM as audio/raw can make some providers (Deepgram)
    silently return an empty transcript.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class WakeWordDispatcher:
    """Reads wake-word detections from Redis and dispatches plugin events."""

    def __init__(self, redis_client: redis.Redis, plugin_router: PluginRouter):
        """Initialize the dispatcher.

        Args:
            redis_client: Async Redis client (decode_responses=False).
            plugin_router: The initialized plugin router.
        """
        self.redis_client = redis_client
        self.plugin_router = plugin_router
        self.consumer_name = "wakeword-dispatch-worker"
        self.running = False

    async def stop(self) -> None:
        """Signal the dispatcher loop to stop."""
        self.running = False

    async def _setup_group(self) -> None:
        try:
            await self.redis_client.xgroup_create(
                DETECTIONS_STREAM, GROUP_NAME, "0", mkstream=True
            )
            logger.info(f"Created consumer group {GROUP_NAME} for {DETECTIONS_STREAM}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def run(self) -> None:
        """Run the consume + dispatch loop until stopped."""
        await self._setup_group()
        self.running = True
        logger.info(f"🔔 WakeWordDispatcher listening on {DETECTIONS_STREAM}")

        while self.running:
            # Heartbeat so the workers healthcheck can tell this loop is turning.
            await beat(self.redis_client, "wakeword-dispatch")
            try:
                messages = await self.redis_client.xreadgroup(
                    GROUP_NAME,
                    self.consumer_name,
                    {DETECTIONS_STREAM: ">"},
                    count=10,
                    block=1000,
                )
            except Exception as e:  # noqa: BLE001 - keep the loop alive on Redis blips
                logger.error(f"WakeWordDispatcher read error: {e}", exc_info=True)
                await asyncio.sleep(1)
                continue

            if not messages:
                continue

            for _stream, stream_messages in messages:
                for message_id, fields in stream_messages:
                    msg_id = (
                        message_id.decode()
                        if isinstance(message_id, bytes)
                        else message_id
                    )
                    try:
                        await self._handle_message(fields)
                    except Exception as e:  # noqa: BLE001 - never let one bad msg stall
                        logger.error(
                            f"Failed to dispatch wake-word detection {msg_id}: {e}",
                            exc_info=True,
                        )
                    finally:
                        await self.redis_client.xack(
                            DETECTIONS_STREAM, GROUP_NAME, msg_id
                        )

    async def _handle_message(self, fields: dict) -> None:
        raw = fields.get(b"event") or fields.get("event")
        if not raw:
            logger.warning("wake-word detection message missing 'event' field")
            return
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)

        session_id = payload.get("session_id", "")
        user_id = payload.get("user_id", "")

        if not user_id:
            logger.warning(
                f"wake-word detection for session '{session_id}' has no user_id; skipping"
            )
            return

        # Resolve the command text from the captured turn. The source is
        # configurable (backend.wakeword.command_source):
        #   batch                -> batch-transcribe the captured audio (default-quality)
        #   streaming            -> trust the live streaming transcript, skip batch ASR
        #   batch_then_streaming -> batch ASR, fall back to streaming with a warning
        audio_b64 = payload.get("audio_b64", "")
        sample_rate = int(payload.get("sample_rate", 16000))
        detected_at = float(payload.get("detected_at") or 0.0)
        command_source = get_wakeword_command_source()
        # Silence gate: the wakeword service flags captures that contained no real
        # speech (a false arm with nothing said). Skip batch ASR on those — self-
        # diarizing ASR (e.g. VibeVoice) hallucinates phantom commands on near-
        # silent audio, which would then be acted on as a real command.
        has_speech = bool(payload.get("has_speech", True))
        command = ""
        # Pre-dispatch stage durations, threaded into the executor's latency line:
        # the captured command-audio length (the wakeword-service arm→end-of-turn
        # window) and the batch ASR wall time.
        capture_secs: float | None = None
        asr_ms: float | None = None
        # Why the command is (or isn't) populated, so downstream consumers can tell
        # an intentional silence-gate skip apart from an ASR miss or missing audio:
        #   "transcribed"       -> batch ASR ran (command may still be empty)
        #   "streaming"         -> taken from the live streaming transcript (by config)
        #   "streaming_fallback"-> batch ASR failed/empty; fell back to streaming
        #   "asr_error"         -> batch ASR was unreachable / errored and no fallback
        #   "skipped_silence"   -> near-silent false arm; ASR deliberately not run
        #   "no_audio"          -> capture carried no audio at all
        if not audio_b64:
            asr_status = "no_audio"
            logger.warning(f"wake-word detection for '{session_id}' carried no audio")
        elif not has_speech:
            asr_status = "skipped_silence"
            logger.info(
                f"wake-word detection for '{session_id}' was near-silent; "
                f"skipping batch ASR (silence gate)"
            )
        else:
            pcm = base64.b64decode(audio_b64)
            # int16 mono: 2 bytes/sample. Used to align the streaming-transcript
            # window against the detection's wall-clock `detected_at`.
            capture_secs = len(pcm) / 2 / max(sample_rate, 1)
            _asr_start = time.perf_counter()
            command, asr_status = await self._resolve_command(
                command_source=command_source,
                pcm=pcm,
                sample_rate=sample_rate,
                session_id=session_id,
                user_id=user_id,
                detected_at=detected_at,
                capture_secs=capture_secs,
            )
            asr_ms = (time.perf_counter() - _asr_start) * 1000.0

        conversation_id = await get_current_conversation_id(
            self.redis_client, session_id
        )
        client_id = payload.get("client_id", session_id)

        # Funnel through the shared executor so the acoustic wake path and the
        # streaming follow-up path dispatch, reply, emit SSE, and arm the
        # follow-up window identically.
        await execute_voice_command(
            self.redis_client,
            self.plugin_router,
            user_id=user_id,
            session_id=session_id,
            client_id=client_id,
            command=command,
            conversation_id=conversation_id,
            source="wake",
            asr_status=asr_status,
            has_speech=has_speech,
            wakeword=payload.get("wakeword"),
            also_fired=payload.get("also_fired", []),
            score=payload.get("score"),
            reason=payload.get("reason"),
            capture_secs=capture_secs,
            asr_ms=asr_ms,
        )

    async def _resolve_command(
        self,
        *,
        command_source: str,
        pcm: bytes,
        sample_rate: int,
        session_id: str,
        user_id: str,
        detected_at: float,
        capture_secs: float,
    ) -> tuple[str, str]:
        """Resolve the command text per the configured source. Returns (command, asr_status)."""
        if command_source == "streaming":
            command = await self._streaming_command(
                session_id, detected_at, capture_secs
            )
            if not command:
                logger.warning(
                    "⚠️ Wake-word command source is 'streaming' but no streaming "
                    "transcript was found for '%s' in the capture window",
                    session_id,
                )
            return command, "streaming"

        # batch or batch_then_streaming: try batch ASR first.
        try:
            command = await self._transcribe(pcm, sample_rate)
            asr_status = "transcribed"
        except Exception as e:  # noqa: BLE001 - decide fallback below
            logger.error(f"Wake-word command transcription failed: {e}", exc_info=True)
            command = ""
            asr_status = "asr_error"

        if command or command_source != "batch_then_streaming":
            return command, asr_status

        # Batch ASR was unreachable or heard nothing — fall back to the live
        # streaming transcript the user already saw, and flag it loudly.
        fallback = await self._streaming_command(session_id, detected_at, capture_secs)
        if fallback:
            logger.warning(
                "⚠️ Wake-word batch ASR %s for '%s'; falling back to the live "
                "streaming transcript: %r",
                "errored" if asr_status == "asr_error" else "returned empty",
                session_id,
                fallback,
            )
            # Surface the degraded path to the UI (best-effort).
            await publish_sse(
                self.redis_client,
                user_id,
                "wake.warning",
                {
                    "session_id": session_id,
                    "reason": "batch_asr_unavailable",
                    "detail": (
                        "Batch ASR was unavailable; used the live streaming "
                        "transcript for this command."
                    ),
                },
            )
            return fallback, "streaming_fallback"

        # Nothing from batch and nothing from streaming either.
        return "", asr_status

    async def _transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Batch-transcribe captured command PCM via the configured STT provider.

        Raises on a missing/unreachable provider so the caller can decide whether
        to fall back to the streaming transcript; returns the (possibly empty)
        transcript when the provider ran successfully.
        """
        if not pcm:
            return ""
        provider = get_transcription_provider(mode="batch")
        if provider is None:
            raise RuntimeError("No batch transcription provider configured")
        # Wake-word command clips are latency-sensitive — use the ASR service's
        # dedicated priority lane so they don't queue behind a long batch.
        result = await provider.transcribe(
            _pcm_to_wav(pcm, sample_rate), sample_rate, priority=True
        )
        return (result.get("text") or "").strip()

    def _strip_wake_words(self, text: str) -> str:
        """Remove a leading/embedded spoken wake word from a streaming transcript."""
        stripped = text
        for wake_word in _SPOKEN_WAKE_WORDS:
            stripped = extract_command_around_keyword(stripped, wake_word)
        return stripped.strip()

    async def _streaming_command(
        self, session_id: str, detected_at: float, capture_secs: float
    ) -> str:
        """Best-effort command from the live streaming transcript for the capture window.

        Streaming final results carry a wall-clock timestamp on the same clock as
        the detection's ``detected_at``, so we keep the finals that land within the
        capture window and strip the wake word. Polls briefly because the final
        streaming result can arrive a beat after the wake-word turn-end fires.
        """
        aggregator = TranscriptionResultsAggregator(self.redis_client)
        have_clock = detected_at > 0
        low = detected_at - capture_secs - _STREAMING_WINDOW_MARGIN_SECS
        high = detected_at + _STREAMING_WINDOW_MARGIN_SECS
        attempts = max(1, int(_STREAMING_POLL_SECS / _STREAMING_POLL_INTERVAL))

        for attempt in range(attempts):
            results = await aggregator.get_session_results(session_id)
            if results:
                if have_clock:
                    windowed = [
                        r
                        for r in results
                        if low <= r.get("timestamp", 0.0) <= high
                        and (r.get("text") or "").strip()
                    ]
                else:
                    # No detection clock to align against; use the latest final only.
                    last = results[-1]
                    windowed = [last] if (last.get("text") or "").strip() else []
                if windowed:
                    text = " ".join((r.get("text") or "").strip() for r in windowed)
                    return self._strip_wake_words(text.strip())
            if attempt < attempts - 1:
                await asyncio.sleep(_STREAMING_POLL_INTERVAL)
        return ""
