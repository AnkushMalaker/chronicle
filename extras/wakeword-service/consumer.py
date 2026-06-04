"""Redis stream consumer for the Hermes wake-word service.

Consumes ``audio:stream:*`` with a dedicated consumer group
``wakeword_detection`` (registered in the backend's ``_EXPECTED_GROUPS`` so the
streaming consumer won't delete streams out from under our pending entries).

For each client stream it maintains per-client wake state in
:class:`HermesDetector`. On a captured turn it resolves the command text from
the existing ``transcription:results:{session_id}`` stream (no second ASR) and
publishes a ``wake_word.detected`` message to the ``wakeword:detections`` stream
for the backend-side dispatcher to forward to the Hermes plugin.
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import Dict

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from detector import SAMPLE_RATE, ClientWakeState, HermesDetector, WakeEvent
from samples import PENDING, POSITIVE, SampleStore

logger = logging.getLogger(__name__)

STREAM_PATTERN = "audio:stream:*"
GROUP_NAME = "wakeword_detection"
DETECTIONS_STREAM = "wakeword:detections"

# Stop processing a stream after this long with no new chunks (zombie guard).
STREAM_IDLE_TIMEOUT_SECONDS = 300


class WakeWordConsumer:
    """Discovers audio streams and runs acoustic wake detection on each."""

    def __init__(
        self, detector: HermesDetector, redis_url: str, sample_store: SampleStore
    ):
        """Initialize the consumer.

        Args:
            detector: Loaded :class:`HermesDetector`.
            redis_url: Redis connection URL (shared with the backend).
            sample_store: Store for captured wake-word clips (the training loop).
        """
        self.detector = detector
        self.redis_url = redis_url
        self.sample_store = sample_store
        self.redis_client: redis.Redis | None = None
        self.consumer_name = f"wakeword-worker-{os.getpid()}"
        self.running = False
        # client_id -> asyncio.Task processing that stream
        self._stream_tasks: Dict[str, asyncio.Task] = {}
        # client_id -> live wake state, so HTTP handlers can prime a stream.
        self._states: Dict[str, ClientWakeState] = {}

    def active_clients(self) -> list[dict]:
        """List currently-processing streams (for the data-collection UI)."""
        out = []
        for client_id, task in self._stream_tasks.items():
            if task.done():
                continue
            state = self._states.get(client_id)
            out.append(
                {
                    "client_id": client_id,
                    "priming": bool(state and state.priming),
                    "armed": bool(state and state.armed),
                }
            )
        return out

    def prime(self, client_id: str) -> bool:
        """Arm a one-shot positive capture on an active stream. False if unknown."""
        state = self._states.get(client_id)
        task = self._stream_tasks.get(client_id)
        if state is None or task is None or task.done():
            return False
        self.detector.start_priming(state, client_id)
        return True

    async def start(self) -> None:
        """Connect to Redis and run the discovery + processing loop."""
        self.redis_client = redis.from_url(self.redis_url)
        self.running = True
        logger.info(
            f"WakeWordConsumer started (group={GROUP_NAME}, redis={self.redis_url})"
        )
        try:
            while self.running:
                await self._discover_and_spawn()
                await asyncio.sleep(2.0)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Signal the consumer to stop."""
        self.running = False

    async def _discover_and_spawn(self) -> None:
        streams = await self._discover_streams()
        for stream_name in streams:
            client_id = stream_name.replace("audio:stream:", "")
            task = self._stream_tasks.get(client_id)
            if task is None or task.done():
                if task is not None and task.done():
                    # Surface any exception from the finished task.
                    exc = task.exception()
                    if exc is not None:
                        logger.error(f"Stream task for '{client_id}' failed: {exc}")
                self._stream_tasks[client_id] = asyncio.create_task(
                    self._process_stream(stream_name, client_id)
                )

    async def _discover_streams(self) -> list[str]:
        streams: list[str] = []
        cursor = b"0"
        while cursor:
            cursor, keys = await self.redis_client.scan(
                cursor, match=STREAM_PATTERN, count=100
            )
            streams.extend(k.decode() if isinstance(k, bytes) else k for k in keys)
        return streams

    async def _setup_group(self, stream_name: str) -> None:
        try:
            await self.redis_client.xgroup_create(
                stream_name, GROUP_NAME, "0", mkstream=True
            )
            logger.debug(f"Created group {GROUP_NAME} for {stream_name}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def _process_stream(self, stream_name: str, client_id: str) -> None:
        await self._setup_group(stream_name)
        state = self.detector.new_client_state()
        self._states[client_id] = state
        session_id = client_id  # session_id == client_id in this pipeline
        last_activity = time.time()
        logger.info(f"▶ Processing wake stream '{stream_name}'")

        try:
            while self.running:
                messages = await self.redis_client.xreadgroup(
                    GROUP_NAME,
                    self.consumer_name,
                    {stream_name: ">"},
                    count=10,
                    block=1000,
                )

                if not messages:
                    if time.time() - last_activity > STREAM_IDLE_TIMEOUT_SECONDS:
                        await self._flush(state, client_id, session_id)
                        logger.info(
                            f"Stream '{stream_name}' idle — ending wake processing"
                        )
                        return
                    continue

                for _stream, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        msg_id = (
                            message_id.decode()
                            if isinstance(message_id, bytes)
                            else message_id
                        )
                        try:
                            if fields.get(b"end_marker") or fields.get("end_marker"):
                                await self.redis_client.xack(
                                    stream_name, GROUP_NAME, msg_id
                                )
                                await self._flush(state, client_id, session_id)
                                logger.info(f"End marker on '{stream_name}' — ending")
                                return

                            pcm = fields.get(b"audio_data") or fields.get("audio_data")
                            if pcm:
                                last_activity = time.time()
                                was_armed = state.armed
                                event = await self.detector.process_frame(
                                    state, client_id, session_id, pcm
                                )
                                # Real acoustic arm transition -> push an immediate
                                # UI pulse (skip deliberate training primes).
                                if state.armed and not was_armed and not state.priming:
                                    await self._on_armed(state, client_id, session_id)
                                if event is not None:
                                    await self._handle_event(event)
                        finally:
                            await self.redis_client.xack(
                                stream_name, GROUP_NAME, msg_id
                            )
        finally:
            self._states.pop(client_id, None)

    async def _flush(self, state, client_id: str, session_id: str) -> None:
        """Finalize an armed-but-uncaptured turn when the stream ends/goes idle."""
        event = self.detector.flush(state, client_id, session_id)
        if event is not None:
            await self._handle_event(event)

    async def _handle_event(self, event: WakeEvent) -> None:
        """Route a captured event: persist training data and (for real arms)
        dispatch the command to the Hermes plugin via Redis."""
        if event.kind == "primed_positive":
            # Known-good wake utterance from the "prime + say it" flow -> positive set.
            self._save_sample(POSITIVE, event, event.audio)
            return
        # Real acoustic arm: snapshot the trigger window for false-positive review,
        # then forward the command turn to Hermes exactly as before.
        self._save_sample(PENDING, event, event.trigger_audio)
        await self._publish_detection(event)

    def _save_sample(self, bucket: str, event: WakeEvent, pcm: bytes) -> None:
        """Persist a captured clip into the on-disk training store."""
        if not pcm:
            return
        meta = {
            "client_id": event.client_id,
            "session_id": event.session_id,
            "score": round(event.score, 4),
            "reason": event.reason,
            "kind": event.kind,
            "source": "prime" if event.kind == "primed_positive" else "arm",
        }
        if event.kind == "primed_positive":
            meta["false_negative"] = event.is_false_negative
            meta["label"] = "wake"
        try:
            rec = self.sample_store.save(
                bucket, pcm, SAMPLE_RATE, int(time.time() * 1000), meta
            )
            logger.info(f"💾 saved {bucket} sample {rec['id']} ({len(pcm)}B)")
        except (
            Exception
        ) as e:  # noqa: BLE001 - data collection must never break dispatch
            logger.error(f"Failed to save {bucket} sample: {e}", exc_info=True)

    async def _publish_detection(self, event: WakeEvent) -> None:
        """Publish a wake_word.detected message carrying the captured command audio.

        The backend dispatcher batch-transcribes ``audio_b64`` for a higher-quality
        command than the streaming transcript would give, and avoids fragile
        timestamp alignment against the transcription results stream.
        """
        user_id = await self._lookup_user_id(event.session_id)
        # Immediate UI pulse for end-of-turn, before the (slower) batch ASR + plugin
        # dispatch the backend does — keeps the live-recording feedback snappy.
        await self._publish_sse(
            user_id,
            "wake.end_of_turn",
            {
                "client_id": event.client_id,
                "session_id": event.session_id,
                "reason": event.reason,
                "duration": round(event.eot_time - event.arm_time, 2),
            },
        )
        # Play the "processing" tone on the device the moment the turn ends.
        await self._publish_downlink(event.client_id, "play-tone", {"tone": "done"})
        payload = {
            "client_id": event.client_id,
            "session_id": event.session_id,
            "user_id": user_id,
            "score": round(event.score, 4),
            "reason": event.reason,
            "sample_rate": SAMPLE_RATE,
            "audio_b64": base64.b64encode(event.audio).decode("ascii"),
            "detected_at": time.time(),
        }
        await self.redis_client.xadd(
            DETECTIONS_STREAM,
            {b"event": json.dumps(payload).encode()},
            maxlen=200,
            approximate=True,
        )
        logger.info(
            f"📤 Published wake_word.detected for '{event.client_id}' "
            f"({len(event.audio)}B audio, reason={event.reason})"
        )

    async def _on_armed(
        self, state: ClientWakeState, client_id: str, session_id: str
    ) -> None:
        """Push a UI pulse the instant the wake word arms (before capture/ASR)."""
        user_id = await self._lookup_user_id(session_id)
        await self._publish_sse(
            user_id,
            "wake.armed",
            {
                "client_id": client_id,
                "session_id": session_id,
                "score": round(getattr(state, "arm_score", 0.0), 4),
            },
        )
        # Play the "listening" tone on the device the instant the wake word arms.
        await self._publish_downlink(client_id, "play-tone", {"tone": "armed"})
        logger.info(f"🔔 wake.armed SSE for '{client_id}'")

    async def _publish_downlink(
        self, client_id: str, msg_type: str, data: dict
    ) -> None:
        """Push a control message to the device via ``device:downlink:{client_id}``.

        The backend's WebSocket handler subscribes to this channel and forwards the
        frame down to the HAVPE relay, which plays it on the device. Best-effort —
        a missing/audio-only device just ignores it.
        """
        if not client_id:
            return
        try:
            message = json.dumps({"type": msg_type, "data": data})
            await self.redis_client.publish(f"device:downlink:{client_id}", message)
        except (
            Exception
        ) as e:  # noqa: BLE001 - downlink is best-effort, never break dispatch
            logger.debug(f"Failed to publish downlink {msg_type}: {e}")

    async def _publish_sse(self, user_id: str, event_type: str, data: dict) -> None:
        """Publish an SSE event to the user's channel (best-effort, never raises).

        Mirrors the backend ``sse_publisher`` wire format ({event, data, timestamp})
        so the ``/api/events/stream`` endpoint relays it to the browser unchanged.
        """
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

    async def _lookup_user_id(self, session_id: str) -> str:
        """Read user_id from the session metadata hash."""
        try:
            val = await self.redis_client.hget(f"audio:session:{session_id}", "user_id")
            if val is not None:
                return val.decode() if isinstance(val, bytes) else val
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read user_id for {session_id}: {e}")
        return ""

    async def _shutdown(self) -> None:
        for task in self._stream_tasks.values():
            task.cancel()
        if self.redis_client is not None:
            await self.redis_client.aclose()
        logger.info("WakeWordConsumer stopped")
