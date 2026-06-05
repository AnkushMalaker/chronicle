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
import time
import wave

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.transcription import get_transcription_provider
from advanced_omi_backend.services.tts_client import synthesize_speech

logger = logging.getLogger(__name__)

DETECTIONS_STREAM = "wakeword:detections"
GROUP_NAME = "wakeword-dispatch"


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

        # Batch-transcribe the captured command audio (higher quality than the
        # streaming transcript). Empty command -> the Hermes plugin reports it.
        audio_b64 = payload.get("audio_b64", "")
        sample_rate = int(payload.get("sample_rate", 16000))
        # Silence gate: the wakeword service flags captures that contained no real
        # speech (a false arm with nothing said). Skip batch ASR on those — self-
        # diarizing ASR (e.g. VibeVoice) hallucinates phantom commands on near-
        # silent audio, which would then be acted on as a real command.
        has_speech = bool(payload.get("has_speech", True))
        command = ""
        # Why the command is (or isn't) populated, so downstream consumers can tell
        # an intentional silence-gate skip apart from an ASR miss or missing audio:
        #   "transcribed"  -> batch ASR ran (command may still be empty if it heard
        #                     nothing meaningful)
        #   "skipped_silence" -> near-silent false arm; ASR deliberately not run
        #   "no_audio"     -> capture carried no audio at all
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
            asr_status = "transcribed"
            command = await self._transcribe(base64.b64decode(audio_b64), sample_rate)

        conversation_id = await self._current_conversation_id(session_id)

        data = {
            "command": command,
            "client_id": payload.get("client_id", session_id),
            "session_id": session_id,
            "conversation_id": conversation_id,
            "score": payload.get("score"),
            "reason": payload.get("reason"),
            "asr_status": asr_status,
            "transcript": command,  # alias for plugins that read transcript
        }

        logger.info(
            f"🔔 Dispatching wake_word.detected (user={user_id}, "
            f"session={session_id}, asr_status={asr_status}, command='{command[:50]}')"
        )
        results = await self.plugin_router.dispatch_event(
            event=PluginEvent.WAKE_WORD_DETECTED,
            user_id=user_id,
            data=data,
            metadata={"client_id": payload.get("client_id", session_id)},
        )

        # Surface the recognized command (+ Hermes reply) on the live-recording UI.
        reply = next((r.message for r in results if getattr(r, "message", None)), "")
        client_id = payload.get("client_id", session_id)
        await self._publish_sse(
            user_id,
            "wake.command",
            {
                "command": command,
                "reply": reply,
                "conversation_id": conversation_id,
                "client_id": client_id,
                "asr_status": asr_status,
            },
        )

        # Speak the reply on the device (best-effort). The device can't reach the
        # backend, so we ship the synthesized audio bytes down the downlink channel
        # and the relay serves them on the LAN.
        if reply:
            await self._speak_on_device(client_id, reply)

    async def _speak_on_device(self, client_id: str, text: str) -> None:
        """Synthesize ``text`` and push it to the device via its downlink channel."""
        if not client_id:
            return
        audio = await synthesize_speech(text)
        if not audio:
            return
        msg = {
            "type": "play-audio",
            "data": {
                "audio_b64": base64.b64encode(audio).decode("ascii"),
                "format": "wav",
            },
        }
        try:
            await self.redis_client.publish(
                f"device:downlink:{client_id}", json.dumps(msg)
            )
            logger.info(f"🔊 Sent TTS reply ({len(audio)}B) to device {client_id}")
        except Exception as e:  # noqa: BLE001 - speech output is best-effort
            logger.debug(f"Failed to publish TTS downlink for {client_id}: {e}")

    async def _publish_sse(self, user_id: str, event_type: str, data: dict) -> None:
        """Publish an SSE event to the user's channel (best-effort, never raises)."""
        if not user_id:
            return
        try:
            message = json.dumps(
                {"event": event_type, "data": data, "timestamp": time.time()}
            )
            await self.redis_client.publish(f"sse:{user_id}", message)
        except (
            Exception
        ) as e:  # noqa: BLE001 - SSE is best-effort, never break dispatch
            logger.debug(f"Failed to publish SSE {event_type}: {e}")

    async def _transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Batch-transcribe captured command PCM via the configured STT provider."""
        if not pcm:
            return ""
        provider = get_transcription_provider(mode="batch")
        if provider is None:
            logger.error(
                "No batch transcription provider configured; command will be empty"
            )
            return ""
        try:
            result = await provider.transcribe(
                _pcm_to_wav(pcm, sample_rate), sample_rate
            )
        except (
            Exception
        ) as e:  # noqa: BLE001 - degrade to empty command, keep dispatching
            logger.error(f"Wake-word command transcription failed: {e}", exc_info=True)
            return ""
        return (result.get("text") or "").strip()

    async def _current_conversation_id(self, session_id: str):
        if not session_id:
            return None
        try:
            val = await self.redis_client.get(f"conversation:current:{session_id}")
            if val is not None:
                return val.decode() if isinstance(val, bytes) else val
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read current conversation for {session_id}: {e}")
        return None
