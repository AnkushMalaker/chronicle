"""Redis stream consumer for the Hermes wake-word service.

Consumes ``audio:stream:*`` with a dedicated consumer group
``wakeword_detection`` (registered in the backend's ``_EXPECTED_GROUPS`` so the
streaming consumer won't delete streams out from under our pending entries).

For each session stream it maintains per-session wake state in
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
from pathlib import Path
from typing import Dict

import redis.asyncio as redis
from detector import (
    RECEPTIVE_FIELD_SECONDS,
    SAMPLE_RATE,
    ClientWakeState,
    HermesDetector,
    WakeEvent,
)
from identities import (
    AudioSessionRef,
    AudioStreamName,
    ClientId,
    SessionId,
    audio_session_key,
    device_downlink_channel,
    parse_audio_stream_name,
)
from redis import exceptions as redis_exceptions
from samples import PENDING, SampleStore

logger = logging.getLogger(__name__)

STREAM_PATTERN = "audio:stream:*"
GROUP_NAME = "wakeword_detection"
DETECTIONS_STREAM = "wakeword:detections"

# Notification tones (HA Voice PE sounds, CC-BY 4.0 — see tones/LICENSE.md).
# This service is the single source of tone audio: tones are sent to every client
# (HAVPE relay, phone app, web UI) as inline ``play-audio`` bytes, which they all
# decode and play. No client bundles its own copy.
_TONES_DIR = Path(__file__).resolve().parent / "tones"
_TONE_FILES = {"armed": "armed.wav", "done": "done.wav"}  # logical name -> file


def _load_tones() -> Dict[str, str]:
    """Load each tone as a base64 ``play-audio`` payload once at import."""
    loaded: Dict[str, str] = {}
    for name, filename in _TONE_FILES.items():
        path = _TONES_DIR / filename
        try:
            loaded[name] = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as e:  # noqa: BLE001 - a missing tone must not break detection
            logger.warning("Tone '%s' unavailable (%s): %s", name, path, e)
    return loaded


_TONE_B64 = _load_tones()

# Stop processing a stream after this long with no new chunks (zombie guard).
STREAM_IDLE_TIMEOUT_SECONDS = 300

# Dev: persist the interpreter buffer state at arm (embeddings + ~10 s raw audio)
# alongside each captured clip, so a false positive is exactly reproducible
# offline. On by default; set WAKEWORD_SAVE_BUFFER_STATE=0 to disable.
SAVE_BUFFER_STATE = os.getenv("WAKEWORD_SAVE_BUFFER_STATE", "1").lower() not in (
    "0",
    "false",
    "no",
)


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
        # session_id -> asyncio.Task processing that stream
        self._stream_tasks: Dict[SessionId, asyncio.Task] = {}
        # session_id -> live wake state, so HTTP handlers can prime a stream.
        self._states: Dict[SessionId, ClientWakeState] = {}
        # session_id -> stable device client_id, resolved from session metadata.
        self._client_ids: Dict[SessionId, ClientId] = {}

    def active_clients(self) -> list[dict]:
        """List currently-processing streams (for the data-collection UI)."""
        out = []
        for session_id, task in self._stream_tasks.items():
            if task.done():
                continue
            client_id = self._client_ids.get(session_id)
            state = self._states.get(session_id)
            if client_id is None or state is None:
                continue
            out.append(
                {
                    "client_id": str(client_id),
                    "session_id": str(session_id),
                    "priming": bool(state and state.priming),
                    "prime_wakeword": (
                        state.prime_wakeword if state and state.priming else None
                    ),
                    "armed": bool(state and state.armed),
                }
            )
        return out

    def prime(self, client_id: str, wakeword: str) -> bool:
        """Arm a one-shot positive capture of ``wakeword`` on an active stream.

        Returns False if the stream is unknown. Raises ValueError if ``wakeword``
        is not a configured wake word.
        """
        # A reconnect can briefly leave an older session draining. Walk newest
        # first so the command targets the device's current live stream.
        client_ref = ClientId.from_value(client_id)
        for session_id in reversed(self._stream_tasks):
            if self._client_ids.get(session_id) != client_ref:
                continue
            state = self._states.get(session_id)
            task = self._stream_tasks.get(session_id)
            if state is not None and task is not None and not task.done():
                self.detector.start_priming(state, client_ref, wakeword)
                return True
        return False

    def unprime(self, client_id: str) -> bool:
        """Manually end an in-progress prime capture (UI 'stop'). False if unknown.

        The per-stream task finalizes and saves on its next frame, so the captured
        attempt always lands in the review queue rather than being dropped.
        """
        client_ref = ClientId.from_value(client_id)
        for session_id in reversed(self._stream_tasks):
            if self._client_ids.get(session_id) != client_ref:
                continue
            state = self._states.get(session_id)
            task = self._stream_tasks.get(session_id)
            if state is not None and task is not None and not task.done():
                self.detector.stop_priming(state)
                return True
        return False

    async def start(self) -> None:
        """Connect to Redis and run the discovery + processing loop."""
        self.redis_client = redis.from_url(self.redis_url)
        self.running = True
        logger.info(
            f"WakeWordConsumer started (group={GROUP_NAME}, redis={self.redis_url})"
        )
        try:
            while self.running:
                # A transient Redis failure (e.g. Redis restarting during a stack
                # restart) must NOT permanently kill the discovery loop — otherwise
                # the HTTP server stays up reporting "healthy" while no audio is ever
                # consumed again. Catch, log, back off, and retry; the redis.asyncio
                # connection pool reconnects on the next command.
                try:
                    await self._discover_and_spawn()
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    raise
                except redis_exceptions.RedisError as e:
                    logger.warning(
                        f"Redis error in discovery loop (retrying in 2s): {e}"
                    )
                    await asyncio.sleep(2.0)
                except Exception as e:  # noqa: BLE001 - loop must never die silently
                    logger.error(
                        f"Unexpected error in discovery loop (retrying in 2s): {e}",
                        exc_info=True,
                    )
                    await asyncio.sleep(2.0)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Signal the consumer to stop."""
        self.running = False

    async def _discover_and_spawn(self) -> None:
        streams = await self._discover_streams()
        live_sessions: set[SessionId] = set()
        for raw_stream_name in streams:
            stream_name = AudioStreamName.from_value(raw_stream_name)
            session_id = parse_audio_stream_name(stream_name)
            # A device that drops without a clean end-marker leaves its
            # audio:stream key behind (session stuck "active"). Without this
            # check we'd re-spawn a task for that dead key every time the
            # previous one idled out, so it perpetually shows as an "active
            # stream". Only process streams that got a chunk recently.
            if not await self._stream_is_live(stream_name):
                continue
            live_sessions.add(session_id)
            task = self._stream_tasks.get(session_id)
            if task is None or task.done():
                if task is not None and task.done():
                    # Surface any exception from the finished task.
                    exc = task.exception()
                    if exc is not None:
                        logger.error(f"Stream task for '{session_id}' failed: {exc}")
                self._stream_tasks[session_id] = asyncio.create_task(
                    self._process_stream(stream_name, session_id)
                )
        # Reap tasks whose stream is gone or has gone stale, so they stop being
        # reported as active streams (and free their per-client detector state).
        for session_id, task in list(self._stream_tasks.items()):
            if task.done():
                self._stream_tasks.pop(session_id, None)
                self._client_ids.pop(session_id, None)
            elif session_id not in live_sessions:
                logger.info(f"Reaping wake stream task for stale '{session_id}'")
                task.cancel()
                self._stream_tasks.pop(session_id, None)
                self._client_ids.pop(session_id, None)

    async def _stream_is_live(self, stream_name: AudioStreamName) -> bool:
        """True if the stream received a chunk within the idle window.

        Redis stream entry ids are wall-clock-ms based (server-assigned on XADD),
        so the last entry's id tells us how long ago audio last arrived — the
        signal that distinguishes a live stream from an abandoned one whose key
        Redis still holds.
        """
        try:
            entries = await self.redis_client.xrevrange(str(stream_name), count=1)
        except redis_exceptions.ResponseError:
            return False
        if not entries:
            return False
        last_id = entries[0][0]
        last_id = last_id.decode() if isinstance(last_id, bytes) else last_id
        try:
            ts_ms = int(last_id.split("-")[0])
        except (ValueError, IndexError):
            return False
        return (time.time() * 1000 - ts_ms) < STREAM_IDLE_TIMEOUT_SECONDS * 1000

    async def _discover_streams(self) -> list[str]:
        streams: list[str] = []
        cursor = b"0"
        while cursor:
            cursor, keys = await self.redis_client.scan(
                cursor, match=STREAM_PATTERN, count=100
            )
            streams.extend(k.decode() if isinstance(k, bytes) else k for k in keys)
        return streams

    async def _setup_group(self, stream_name: AudioStreamName) -> None:
        try:
            await self.redis_client.xgroup_create(
                str(stream_name), GROUP_NAME, "0", mkstream=True
            )
            logger.debug(f"Created group {GROUP_NAME} for {stream_name}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def _process_stream(
        self, stream_name: AudioStreamName, session_id: SessionId
    ) -> None:
        await self._setup_group(stream_name)
        session_ref = await self._lookup_audio_session_ref(session_id)
        state = self.detector.new_client_state()
        self._states[session_id] = state
        self._client_ids[session_id] = session_ref.client_id
        last_activity = time.time()
        logger.info(
            f"▶ Processing wake stream '{stream_name}' for client '{session_ref.client_id}'"
        )

        try:
            while self.running:
                messages = await self.redis_client.xreadgroup(
                    GROUP_NAME,
                    self.consumer_name,
                    {str(stream_name): ">"},
                    count=10,
                    block=1000,
                )

                if not messages:
                    if time.time() - last_activity > STREAM_IDLE_TIMEOUT_SECONDS:
                        await self._flush(state, session_ref)
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
                                    str(stream_name), GROUP_NAME, msg_id
                                )
                                await self._flush(state, session_ref)
                                logger.info(f"End marker on '{stream_name}' — ending")
                                return

                            pcm = fields.get(b"audio_data") or fields.get("audio_data")
                            if pcm:
                                last_activity = time.time()
                                was_armed = state.armed
                                event = await self.detector.process_frame(
                                    state, session_ref, pcm
                                )
                                # Real acoustic arm transition -> push an immediate
                                # UI pulse (skip deliberate training primes).
                                if state.armed and not was_armed and not state.priming:
                                    await self._on_armed(state, session_ref)
                                if event is not None:
                                    await self._handle_event(event)
                        finally:
                            await self.redis_client.xack(
                                str(stream_name), GROUP_NAME, msg_id
                            )
        finally:
            self._states.pop(session_id, None)
            self._client_ids.pop(session_id, None)

    async def _flush(self, state, session_ref: AudioSessionRef) -> None:
        """Finalize an armed-but-uncaptured turn when the stream ends/goes idle."""
        event = self.detector.flush(state, session_ref)
        if event is not None:
            await self._handle_event(event)

    async def _handle_event(self, event: WakeEvent) -> None:
        """Route a captured event: persist training data and (for real arms)
        dispatch the command to the Hermes plugin via Redis."""
        if event.kind == "primed_positive":
            # "Prime + say it" capture -> review queue (pending), same as a real
            # arm, so the user confirms wake / not-wake before it rolls into
            # training. Rescued false-negatives become positives once labeled.
            # Off-loop: the disk write must not stall the per-client frame loop.
            await asyncio.to_thread(self._save_sample, PENDING, event, event.audio)
            return
        # Real acoustic arm. Play the end-of-listening tone FIRST — it's a pure ack
        # that needs only client_id, so it must never wait behind the disk write /
        # user lookup / SSE / XADD below (a slow/near-full disk would otherwise make
        # the tone lag). Collect-only (shadow) arms farm FP data silently: no tone,
        # no command dispatch.
        if not getattr(event, "collect_only", False):
            await self._send_tone(event.client_id, "done")
        # Snapshot the trigger window for false-positive review. Off-loop so the
        # synchronous WAV/JSON writes don't block the frame loop.
        await asyncio.to_thread(self._save_sample, PENDING, event, event.trigger_audio)
        if getattr(event, "collect_only", False):
            return
        await self._publish_detection(event)

    def _save_sample(self, bucket: str, event: WakeEvent, pcm: bytes) -> None:
        """Persist a captured clip into the on-disk training store, scoped to the
        event's wake word."""
        if not pcm:
            return
        meta = {
            "client_id": str(event.client_id),
            "session_id": str(event.session_id),
            "score": round(event.score, 4),
            "reason": event.reason,
            "kind": event.kind,
            "also_fired": list(event.also_fired),
            "collect_only": getattr(event, "collect_only", False),
            "source": (
                "prime"
                if event.kind == "primed_positive"
                else ("shadow" if getattr(event, "collect_only", False) else "arm")
            ),
            # The model's receptive field is ~1.96 s, and the arm fires at the END
            # of the pre-roll, so the activation is the LAST ~1.96 s of this clip.
            "activation_window_secs": RECEPTIVE_FIELD_SECONDS,
            "activation_at": "end",
        }
        if event.kind == "primed_positive":
            # Left UNLABELED so it surfaces in the pending review queue; the score
            # still flags whether the live model under-scored this utterance.
            meta["false_negative"] = event.is_false_negative
        else:
            # Real arm: record whether the captured turn held speech. A near-silent
            # arm (has_speech=False) is a false positive whose command ASR the
            # backend skipped — flagged here so it's visible in the review store.
            meta["has_speech"] = event.has_speech
        # Dev: attach the interpreter buffer state captured at arm so the FP is
        # exactly reproducible offline (command arms only carry it).
        features = event.buffer_features if SAVE_BUFFER_STATE else None
        context_pcm = event.buffer_context if SAVE_BUFFER_STATE else None
        try:
            rec = self.sample_store.save(
                event.wakeword,
                bucket,
                pcm,
                SAMPLE_RATE,
                int(time.time() * 1000),
                meta,
                features=features,
                context_pcm=context_pcm,
            )
            logger.info(
                f"💾 saved {bucket} sample {rec['id']} ({len(pcm)}B"
                f"{', +buffer-state' if rec.get('has_buffer_state') else ''})"
            )
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
                "client_id": str(event.client_id),
                "session_id": str(event.session_id),
                "reason": event.reason,
                "duration": round(event.eot_time - event.arm_time, 2),
            },
        )
        # Amber "Thinking" ring on LED-capable devices: capture is done, the backend
        # is now batch-transcribing + dispatching. The executor refreshes this when
        # dispatch actually starts; it reverts on its own if no command follows.
        await self._publish_downlink(
            event.client_id,
            "led-control",
            {
                "effect": "Thinking",
                "r": 1.0,
                "g": 0.45,
                "b": 0.0,
                "brightness": 0.45,
                "duration": 8.0,
            },
        )
        # NOTE: the end-of-listening ("done") tone is played up front in
        # _handle_event, before this bookkeeping, so it never lags under load.
        payload = {
            "client_id": str(event.client_id),
            "session_id": str(event.session_id),
            "user_id": user_id,
            "wakeword": event.wakeword,
            "also_fired": list(event.also_fired),
            "score": round(event.score, 4),
            "reason": event.reason,
            "sample_rate": SAMPLE_RATE,
            "audio_b64": base64.b64encode(event.audio).decode("ascii"),
            "has_speech": event.has_speech,
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
        self, state: ClientWakeState, session_ref: AudioSessionRef
    ) -> None:
        """Push a UI pulse the instant the wake word arms (before capture/ASR)."""
        # Listening tone FIRST — a pure ack needing only client_id, so it never waits
        # behind the user lookup / SSE below (keeps the cue instant under load).
        await self._send_tone(session_ref.client_id, "armed")
        # Cyan "Listening" ring on LED-capable devices (HAVPE). Like the tone it only
        # needs client_id, so it stays snappy; non-LED clients ignore the frame.
        await self._publish_downlink(
            session_ref.client_id,
            "led-control",
            {
                "effect": "Listening For Command",
                "r": 0.09,
                "g": 0.73,
                "b": 0.95,
                "brightness": 0.45,
                "duration": 12.0,
            },
        )
        user_id = await self._lookup_user_id(session_ref.session_id)
        await self._publish_sse(
            user_id,
            "wake.armed",
            {
                "client_id": str(session_ref.client_id),
                "session_id": str(session_ref.session_id),
                "score": round(getattr(state, "arm_score", 0.0), 4),
            },
        )
        logger.info(f"🔔 wake.armed SSE for '{session_ref.client_id}'")

    async def _send_tone(self, client_id: ClientId, tone: str) -> None:
        """Play a notification tone on the device via inline ``play-audio`` bytes.

        ``play-audio`` carries the tone bytes inline, so every client type (HAVPE
        relay, phone app, web UI) can play it the same way — no client needs its own
        bundled copy. Best-effort: a missing tone asset just means no sound.
        """
        audio_b64 = _TONE_B64.get(tone)
        if not audio_b64:
            return
        await self._publish_downlink(
            client_id,
            "play-audio",
            {"audio_b64": audio_b64, "format": "wav", "announcement": True},
        )

    async def _publish_downlink(
        self, client_id: ClientId, msg_type: str, data: dict
    ) -> None:
        """Push a control message to the device via ``device:downlink:{client_id}``.

        The backend's WebSocket handler subscribes to this channel and forwards the
        frame down to the HAVPE relay, which plays it on the device. Best-effort —
        a missing/audio-only device just ignores it.
        """
        if not isinstance(client_id, ClientId):
            raise TypeError("_publish_downlink requires ClientId")
        try:
            message = json.dumps({"type": msg_type, "data": data})
            await self.redis_client.publish(
                str(device_downlink_channel(client_id)), message
            )
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

    async def _lookup_user_id(self, session_id: SessionId) -> str:
        """Read user_id from the session metadata hash."""
        try:
            val = await self.redis_client.hget(
                str(audio_session_key(session_id)), "user_id"
            )
            if val is not None:
                return val.decode() if isinstance(val, bytes) else val
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read user_id for {session_id}: {e}")
        return ""

    async def _lookup_audio_session_ref(self, session_id: SessionId) -> AudioSessionRef:
        """Resolve the stable device id from authoritative session metadata."""
        if self.redis_client is None:
            raise RuntimeError("Redis is not connected")
        value = await self.redis_client.hget(
            str(audio_session_key(session_id)), "client_id"
        )
        if value is None:
            raise RuntimeError(f"Audio session '{session_id}' has no client_id")
        client_id = ClientId.from_value(value, "client_id")
        return AudioSessionRef(session_id=session_id, client_id=client_id)

    async def _shutdown(self) -> None:
        for task in self._stream_tasks.values():
            task.cancel()
        self._stream_tasks.clear()
        self._states.clear()
        self._client_ids.clear()
        if self.redis_client is not None:
            await self.redis_client.aclose()
        logger.info("WakeWordConsumer stopped")
