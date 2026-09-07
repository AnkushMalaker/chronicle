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
from datetime import datetime, timezone

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from backend.config import get_wakeword_command_source
from backend.heartbeat import beat
from backend.models.user import get_user_by_id
from backend.plugins.router import PluginRouter, extract_command_around_keyword
from backend.redis_keys import ClientId, SessionId
from backend.services.audio_stream.aggregator import TranscriptionResultsAggregator
from backend.services.audio_stream.session_store import SessionStore
from backend.services.transcription import get_transcription_provider
from backend.services.wakeword.activations import WakeActivation, WakeActivationStore
from backend.services.wakeword.contracts import WakeDetectionEvent
from backend.services.wakeword.executor import (
    execute_voice_command,
    get_active_conversation_id,
    play_tone_on_device,
    publish_sse,
    set_device_led,
)
from backend.services.wakeword.interaction_ledger import (
    WakeAudioInterval,
    WakeInteractionFact,
    WakeInteractionLedger,
)
from backend.speaker_recognition_client import SpeakerRecognitionClient

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
_PENDING_MIN_IDLE_MS = 30_000


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

    def __init__(
        self,
        redis_client: redis.Redis,
        plugin_router: PluginRouter,
        interaction_ledger: WakeInteractionLedger | None = None,
        activation_store: WakeActivationStore | None = None,
    ):
        """Initialize the dispatcher.

        Args:
            redis_client: Async Redis client (decode_responses=False).
            plugin_router: The initialized plugin router.
        """
        self.redis_client = redis_client
        self.plugin_router = plugin_router
        self.interaction_ledger = interaction_ledger
        self.activations = activation_store or WakeActivationStore(redis_client)
        self.consumer_name = "wakeword-dispatch-worker"
        self.running = False
        # Lazily-built speaker-recognition client, used only by the per-user
        # speaker gate (see _check_speaker_gate). Reused across detections.
        self._speaker_client: SpeakerRecognitionClient | None = None
        # In-flight per-detection handlers. Each detection is processed in its own
        # background task so a slow plugin (the Hermes agent can take tens of
        # seconds) never blocks the loop from reading + dispatching the NEXT wake
        # command (e.g. a quick Home Assistant command). Tracked so they aren't GC'd
        # and can be cancelled on shutdown.
        self._tasks: set[asyncio.Task] = set()

    async def stop(self) -> None:
        """Signal the dispatcher loop to stop and cancel in-flight handlers."""
        self.running = False
        for task in list(self._tasks):
            task.cancel()

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
        await self._recover_pending_once()

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
                    # Process each detection concurrently in its own task: a command
                    # already sent to a slow plugin (Hermes) runs in the background
                    # while we keep listening, so the next command isn't blocked.
                    task = asyncio.create_task(
                        self._handle_message_safe(fields, msg_id)
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def _recover_pending_once(self) -> None:
        """Claim and retry stale unacknowledged detections after worker failure."""
        claimed = await self.redis_client.xautoclaim(
            DETECTIONS_STREAM,
            GROUP_NAME,
            self.consumer_name,
            min_idle_time=_PENDING_MIN_IDLE_MS,
            start_id="0-0",
            count=100,
        )
        rows = claimed[1] if claimed else []
        for message_id, fields in rows:
            msg_id = (
                message_id.decode() if isinstance(message_id, bytes) else message_id
            )
            await self._handle_message_safe(fields, msg_id)

    async def _handle_message_safe(self, fields: dict, msg_id: str) -> None:
        """Run _handle_message, swallowing errors (nothing awaits this task)."""
        try:
            await self._handle_message(fields)
            await self.redis_client.xack(DETECTIONS_STREAM, GROUP_NAME, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let one bad detection crash
            logger.error(
                f"Failed to dispatch wake-word detection {msg_id}: {e}",
                exc_info=True,
            )

    async def _handle_message(self, fields: dict) -> None:
        raw = fields.get(b"event") or fields.get("event")
        if not raw:
            logger.warning("wake-word detection message missing 'event' field")
            return
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)
        if payload.get("kind") == "tone":
            if payload.get("tone") not in {"armed", "done"}:
                raise ValueError("wake tone request has an invalid tone")
            await play_tone_on_device(
                self.redis_client,
                ClientId.from_value(payload.get("client_id")),
                SessionId.from_value(payload.get("session_id"), "session_id"),
                payload["tone"],
            )
            return
        event = WakeDetectionEvent.from_payload(payload)

        session_id = event.session_id
        session_id_value = str(event.session_id)
        client_id = event.client_id
        user_id = str(event.user_id)

        # Resolve the command text from the captured turn. The source is
        # configurable (backend.wakeword.command_source):
        #   batch                -> batch-transcribe the captured audio (default-quality)
        #   streaming            -> trust the live streaming transcript, skip batch ASR
        #   batch_then_streaming -> batch ASR, fall back to streaming with a warning
        audio_b64 = event.audio_b64
        sample_rate = event.sample_rate
        await self._append_interaction_fact(event, "armed", 0)
        await self._append_interaction_fact(event, "end_of_turn", 1)
        end_of_turn_at = event.end_of_turn_at
        command_source = get_wakeword_command_source()
        # Silence gate: the wakeword service flags captures that contained no real
        # speech (a false arm with nothing said). Skip batch ASR on those — self-
        # diarizing ASR (e.g. VibeVoice) hallucinates phantom commands on near-
        # silent audio, which would then be acted on as a real command.
        has_speech = event.has_speech

        # Speaker presence gate (per-user allowlist). When the user has enabled the
        # gate, an acoustic wake word only dispatches a command if one of their
        # selected enrolled speakers is recognized in the captured turn. Run BEFORE
        # ASR so a blocked command also skips transcription. Fail-open on a
        # speaker-service error so an outage never bricks the wake word.
        gate = await self._check_speaker_gate(
            user_id=user_id,
            audio_b64=audio_b64,
            has_speech=has_speech,
            sample_rate=sample_rate,
        )
        if not gate["allowed"]:
            logger.info(
                f"🚫 wake-word command for '{session_id}' blocked by speaker gate "
                f"(reason={gate['reason']}, identified={gate.get('identified')})"
            )
            await publish_sse(
                self.redis_client,
                user_id,
                "wake.blocked",
                {
                    "client_id": str(client_id),
                    "session_id": session_id_value,
                    "reason": gate["reason"],
                    "wakeword": event.wakeword,
                    "identified": gate.get("identified"),
                },
            )
            # Brief red "Error" ring so a blocked wake word reads as rejected.
            await set_device_led(
                self.redis_client, client_id, effect="Error", duration=3.0
            )
            return

        capture_session = await SessionStore(self.redis_client).read(session_id_value)
        if capture_session is not None and capture_session.voice_session_id:
            if has_speech:
                await self.activations.register(
                    WakeActivation(
                        wake_trace_id=event.wake_trace_id,
                        user_id=user_id,
                        client_id=str(client_id),
                        audio_session_id=session_id_value,
                        capture_epoch=event.capture_epoch,
                        wakeword=event.wakeword,
                        armed_at=event.armed_at,
                        end_of_turn_at=event.end_of_turn_at,
                        command_start_ms=event.command_interval.start_ms,
                        command_end_ms=event.command_interval.end_ms,
                    )
                )
            logger.info(
                "Wake activation registered for audio-v2 voice session %s; "
                "the complete committed turn owns command resolution",
                capture_session.voice_session_id,
            )
            return

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
            logger.warning(
                f"wake-word detection for '{session_id_value}' carried no audio"
            )
        elif not has_speech:
            asr_status = "skipped_silence"
            logger.info(
                f"wake-word detection for '{session_id_value}' was near-silent; "
                f"skipping batch ASR (silence gate)"
            )
        else:
            pcm = base64.b64decode(audio_b64)
            # int16 mono: 2 bytes/sample. Used to align the streaming-transcript
            # window against the capture clock's semantic end-of-turn.
            capture_secs = len(pcm) / 2 / max(sample_rate, 1)
            _asr_start = time.perf_counter()
            command, asr_status = await self._resolve_command(
                command_source=command_source,
                pcm=pcm,
                sample_rate=sample_rate,
                session_id=session_id_value,
                user_id=user_id,
                end_of_turn_at=end_of_turn_at,
                capture_secs=capture_secs,
            )
            asr_ms = (time.perf_counter() - _asr_start) * 1000.0

        conversation_id = await get_active_conversation_id(
            self.redis_client, session_id_value
        )
        await self._append_interaction_fact(
            event,
            "command_resolved",
            2,
            occurred_at=time.time(),
            command=command,
            asr_status=asr_status,
        )

        async def record_execution_stage(stage: str, details: dict) -> None:
            ordinal = {"dispatched": 3, "acted": 4, "followup_opened": 10}[stage]
            await self._append_interaction_fact(
                event, stage, ordinal, occurred_at=time.time(), **details
            )

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
            wakeword=event.wakeword,
            also_fired=event.also_fired,
            score=event.score,
            reason=event.reason,
            capture_secs=capture_secs,
            asr_ms=asr_ms,
            wake_trace_id=event.wake_trace_id,
            interaction_stage_callback=record_execution_stage,
        )

    async def _append_interaction_fact(
        self,
        event: WakeDetectionEvent,
        stage: str,
        ordinal: int,
        *,
        occurred_at: float | None = None,
        **payload,
    ) -> None:
        if self.interaction_ledger is None:
            return
        is_arm = stage == "armed"
        interval = event.trigger_interval if is_arm else event.command_interval
        fact_time = occurred_at or (event.armed_at if is_arm else event.end_of_turn_at)
        await self.interaction_ledger.append(
            WakeInteractionFact(
                wake_trace_id=event.wake_trace_id,
                stage=stage,
                ordinal=ordinal,
                occurred_at=datetime.fromtimestamp(fact_time, tz=timezone.utc),
                user_id=str(event.user_id),
                client_id=str(event.client_id),
                audio_session_id=str(event.session_id),
                capture_epoch=event.capture_epoch,
                wakeword=event.wakeword,
                audio_interval=WakeAudioInterval(
                    start_ms=interval.start_ms,
                    end_ms=interval.end_ms,
                    started_at=datetime.fromtimestamp(
                        interval.started_at, tz=timezone.utc
                    ),
                    ended_at=datetime.fromtimestamp(interval.ended_at, tz=timezone.utc),
                ),
                payload=payload,
            )
        )

    async def _check_speaker_gate(
        self,
        *,
        user_id: str,
        audio_b64: str,
        has_speech: bool,
        sample_rate: int,
    ) -> dict:
        """Decide whether this captured turn may dispatch, per the user's gate.

        Returns ``{"allowed": bool, "reason": str, "identified": str | None}``.
        When the user has not enabled the gate (or selected no speakers) it is
        inert and always allows. Otherwise the captured turn is run through the
        speaker-recognition ``/identify`` endpoint and allowed only if the
        identified speaker is in the user's allowlist. A near-silent capture can't
        be verified (blocked); a speaker-service error fails OPEN (allowed).
        """
        try:
            user = await get_user_by_id(user_id)
        except Exception as e:  # noqa: BLE001 - never block dispatch on a lookup blip
            logger.warning(f"speaker gate: user lookup failed for {user_id}: {e}")
            return {"allowed": True, "reason": "user_lookup_failed", "identified": None}

        if user is None or not getattr(user, "wakeword_gate_enabled", False):
            return {"allowed": True, "reason": "gate_off", "identified": None}

        allowed = user.wakeword_allowed_speakers or []
        if not allowed:
            # Enabled but nobody selected — treat as misconfigured and stay inert
            # rather than silently blocking every command.
            logger.warning(
                f"speaker gate enabled for user {user_id} but no speakers selected "
                f"— allowing (gate inert)"
            )
            return {
                "allowed": True,
                "reason": "no_speakers_selected",
                "identified": None,
            }

        if not audio_b64 or not has_speech:
            # Nothing to verify against — a near-silent / empty arm can't be
            # attributed to an allowed speaker, so the gate blocks it.
            return {
                "allowed": False,
                "reason": "unverifiable_silence",
                "identified": None,
            }

        allowed_ids = {str(s.get("speaker_id")) for s in allowed if s.get("speaker_id")}
        allowed_names = {
            str(s.get("name")).strip().lower() for s in allowed if s.get("name")
        }

        if self._speaker_client is None:
            self._speaker_client = SpeakerRecognitionClient()
        if not self._speaker_client.enabled:
            # No speaker service configured — fail open so the gate never bricks
            # the wake word on a misconfigured deployment.
            logger.warning("speaker gate: speaker service not configured — allowing")
            return {
                "allowed": True,
                "reason": "service_unavailable",
                "identified": None,
            }

        wav = _pcm_to_wav(base64.b64decode(audio_b64), sample_rate)
        result = await self._speaker_client.identify_segment(wav, user_id=user_id)

        if result.get("status") == "error":
            logger.warning("speaker gate: /identify errored — allowing (fail-open)")
            return {"allowed": True, "reason": "service_error", "identified": None}

        identified_id = str(result.get("speaker_id") or "")
        identified_name = str(result.get("speaker_name") or "").strip().lower()
        is_allowed = bool(result.get("found")) and (
            identified_id in allowed_ids or identified_name in allowed_names
        )
        return {
            "allowed": is_allowed,
            "reason": "speaker_recognized" if is_allowed else "speaker_not_allowed",
            "identified": result.get("speaker_name"),
        }

    async def _resolve_command(
        self,
        *,
        command_source: str,
        pcm: bytes,
        sample_rate: int,
        session_id: str,
        user_id: str,
        end_of_turn_at: float,
        capture_secs: float,
    ) -> tuple[str, str]:
        """Resolve the command text per the configured source. Returns (command, asr_status)."""
        if command_source == "streaming":
            command = await self._streaming_command(
                session_id, end_of_turn_at, capture_secs
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
        fallback = await self._streaming_command(
            session_id, end_of_turn_at, capture_secs
        )
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
        self, session_id: str, end_of_turn_at: float, capture_secs: float
    ) -> str:
        """Best-effort command from the live streaming transcript for the capture window.

        Streaming final results carry a wall-clock timestamp on the same clock as
        the capture's ``end_of_turn_at``, so we keep finals that land within the
        capture window and strip the wake word. Polls briefly because the final
        streaming result can arrive a beat after the wake-word turn-end fires.
        """
        aggregator = TranscriptionResultsAggregator(self.redis_client)
        low = end_of_turn_at - capture_secs - _STREAMING_WINDOW_MARGIN_SECS
        high = end_of_turn_at + _STREAMING_WINDOW_MARGIN_SECS
        attempts = max(1, int(_STREAMING_POLL_SECS / _STREAMING_POLL_INTERVAL))

        for attempt in range(attempts):
            results = await aggregator.get_session_results(session_id)
            if results:
                windowed = [
                    r
                    for r in results
                    if low <= r.get("timestamp", 0.0) <= high
                    and (r.get("text") or "").strip()
                ]
                if windowed:
                    text = " ".join((r.get("text") or "").strip() for r in windowed)
                    return self._strip_wake_words(text.strip())
            if attempt < attempts - 1:
                await asyncio.sleep(_STREAMING_POLL_INTERVAL)
        return ""
