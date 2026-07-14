"""Shared voice-command executor and follow-up window state.

Both the wake-word dispatcher (acoustic path) and the streaming follow-up handler
funnel resolved commands through :func:`execute_voice_command`, so command
dispatch, the spoken reply, the live-recording SSE event, and the follow-up
window behave identically regardless of how the command arrived.

The follow-up window is a short, per-session Redis key opened only after a
command actually acted on something. While it is open the streaming
follow-up handler treats the next utterance as a contextual follow-up (see
``followup.py``). Executing a follow-up re-opens the window, so follow-ups
chain naturally ("warmer" ... "warmer" ... "a bit cooler").
"""

import base64
import functools
import io
import json
import logging
import math
import os
import struct
import time
import wave
from typing import Any, List, Optional

import redis.asyncio as redis

from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.tts_client import synthesize_speech
from advanced_omi_backend.services.wakeword.timing import WakeTimer

logger = logging.getLogger(__name__)

# How long a follow-up window stays open after a successful command (seconds).
FOLLOWUP_WINDOW_SECS = int(os.getenv("FOLLOWUP_WINDOW_SECS", "12"))


_TONE_SAMPLE_RATE = 16000


def _append_note(
    frames: bytearray,
    freq: float,
    ms: int,
    volume: float,
    *,
    envelope: str = "hann",
    harmonics: Optional[List[tuple]] = None,
) -> None:
    """Append one int16 note to ``frames``.

    ``envelope``: "hann" gives a smooth raised-cosine swell (soft, click-free,
    inviting); "blip" gives a quick 5ms attack/decay (snappy, attention-grabbing).
    ``harmonics`` is a list of ``(freq_multiple, amplitude)`` partials added on top
    of the fundamental — a little 2nd harmonic warms a pure sine without harshness.
    """
    n = int(_TONE_SAMPLE_RATE * ms / 1000)
    if n <= 0:
        return
    partials = harmonics or [(1.0, 1.0)]
    attack = max(1, int(_TONE_SAMPLE_RATE * 0.005))
    for i in range(n):
        if envelope == "hann":
            env = 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1))) if n > 1 else 1.0
        else:  # "blip"
            env = min(1.0, i / attack, (n - i) / attack)
        sample = sum(
            amp * math.sin(2 * math.pi * freq * mult * i / _TONE_SAMPLE_RATE)
            for mult, amp in partials
        )
        sample *= volume * env
        frames.extend(struct.pack("<h", max(-32768, min(32767, int(sample * 32767)))))


def _silence(frames: bytearray, ms: int) -> None:
    frames.extend(b"\x00\x00" * int(_TONE_SAMPLE_RATE * ms / 1000))


def _wav_b64(frames: bytearray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_TONE_SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode("ascii")


@functools.lru_cache(maxsize=1)
def _thinking_tone_b64() -> str:
    """Soft, inviting two-note rising swell for the agentic handoff.

    Played when a fast handler (Home Assistant) declines and a slower one (the
    Hermes agent) takes over, so the unavoidable wait reads as a warm "hold on,
    working on it" rather than an alert. Low register, gentle Hann swells, quiet,
    with a touch of 2nd harmonic for warmth — deliberately the opposite of the
    sharp error tone.
    """
    frames = bytearray()
    # Gentle rising perfect-fourth (G4 → C5), smooth swells, soft.
    _append_note(
        frames, 392.00, 200, 0.20, envelope="hann", harmonics=[(1.0, 1.0), (2.0, 0.12)]
    )
    _silence(frames, 45)
    _append_note(
        frames, 523.25, 260, 0.20, envelope="hann", harmonics=[(1.0, 1.0), (2.0, 0.10)]
    )
    return _wav_b64(frames)


@functools.lru_cache(maxsize=1)
def _error_tone_b64() -> str:
    """Sharp two-blip cue for a mistake / not-found / failure.

    The crisper, more attention-grabbing tone — appropriate when something didn't
    work (command not understood, target not found, a handler errored).
    """
    frames = bytearray()
    _append_note(frames, 660.0, 90, 0.30, envelope="blip")
    _silence(frames, 60)
    _append_note(frames, 880.0, 110, 0.30, envelope="blip")
    return _wav_b64(frames)


# Logical tone name -> base64 WAV generator.
_TONE_GENERATORS = {
    "thinking": _thinking_tone_b64,  # soft handoff to the agentic path
    "error": _error_tone_b64,  # mistake / not-found / failure
}


async def play_tone_on_device(
    redis_client: redis.Redis, client_id: str, tone: str = "thinking"
) -> None:
    """Play a short notification tone on the device via its downlink channel.

    Uses the same inline ``play-audio`` path as :func:`speak_on_device`, so every
    client type (HAVPE relay, phone, web UI) plays it. Best-effort; never raises.
    """
    if not client_id:
        return
    generator = _TONE_GENERATORS.get(tone)
    if generator is None:
        logger.debug(f"Unknown tone '{tone}'; not playing")
        return
    audio_b64 = generator()
    if not audio_b64:
        return
    msg = {"type": "play-audio", "data": {"audio_b64": audio_b64, "format": "wav"}}
    try:
        await redis_client.publish(f"device:downlink:{client_id}", json.dumps(msg))
        logger.info(f"🔔 Sent '{tone}' tone to device {client_id}")
    except Exception as e:  # noqa: BLE001 - tone output is best-effort
        logger.debug(f"Failed to publish '{tone}' tone downlink for {client_id}: {e}")


# LED ring feedback colours (0..1 RGB) the firmware tints its animation with.
# "Error" is hardcoded red on-device, so its colour is irrelevant.
_LED_LISTEN_COLOR = {"r": 0.09, "g": 0.73, "b": 0.95}  # cyan
_LED_THINK_COLOR = {"r": 1.0, "g": 0.45, "b": 0.0}  # amber


async def set_device_led(
    redis_client: redis.Redis,
    client_id: str,
    *,
    effect: str,
    color: Optional[dict] = None,
    brightness: float = 0.45,
    duration: float = 10.0,
) -> None:
    """Drive an LED-capable device's ring to a named effect via its downlink.

    Visual analogue of :func:`play_tone_on_device`: the ``led-control`` frame goes
    to every client type, but only the HAVPE relay acts on it (other clients ignore
    unknown downlink types). The firmware reverts to its connectivity colour after
    ``duration`` seconds. Best-effort; never raises.
    """
    if not client_id:
        return
    data: dict[str, Any] = {
        "effect": effect,
        "brightness": brightness,
        "duration": duration,
    }
    if color:
        data.update(color)
    msg = {"type": "led-control", "data": data}
    try:
        await redis_client.publish(f"device:downlink:{client_id}", json.dumps(msg))
    except Exception as e:  # noqa: BLE001 - LED feedback is best-effort
        logger.debug(f"Failed to publish led-control downlink for {client_id}: {e}")


def _ctx_key(session_id: str) -> str:
    return f"followup:ctx:{session_id}"


def _mute_key(session_id: str) -> str:
    return f"followup:mute:{session_id}"


async def open_followup_window(
    redis_client: redis.Redis, session_id: str, command: str
) -> None:
    """Open/refresh the follow-up window, recording the command just executed."""
    if not session_id:
        return
    payload = json.dumps({"command": command, "ts": time.time()})
    await redis_client.set(_ctx_key(session_id), payload, ex=FOLLOWUP_WINDOW_SECS)


async def get_followup_ctx(
    redis_client: redis.Redis, session_id: str
) -> Optional[dict]:
    """Return the open follow-up context for a session, or None if the window is closed."""
    if not session_id:
        return None
    raw = await redis_client.get(_ctx_key(session_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def clear_followup_window(redis_client: redis.Redis, session_id: str) -> None:
    if not session_id:
        return
    await redis_client.delete(_ctx_key(session_id))


async def is_muted(redis_client: redis.Redis, session_id: str) -> bool:
    """True while the device is (likely) still playing our spoken reply.

    Guards against the device mic capturing the assistant's own TTS reply and
    the streaming transcript treating it as a follow-up.
    """
    if not session_id:
        return False
    return bool(await redis_client.exists(_mute_key(session_id)))


async def _set_mute(redis_client: redis.Redis, session_id: str, secs: float) -> None:
    if not session_id or secs <= 0:
        return
    # Redis EX is integer seconds; round up so short replies still get a floor.
    await redis_client.set(_mute_key(session_id), "1", ex=max(1, int(secs + 0.999)))


async def get_current_conversation_id(
    redis_client: redis.Redis, session_id: str
) -> Optional[str]:
    """Resolve the active conversation id for a session, if any."""
    if not session_id:
        return None
    try:
        val = await redis_client.get(f"conversation:current:{session_id}")
        if val is not None:
            return val.decode() if isinstance(val, bytes) else val
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read current conversation for {session_id}: {e}")
    return None


async def publish_sse(
    redis_client: redis.Redis, user_id: str, event_type: str, data: dict
) -> None:
    """Publish an SSE event to the user's channel (best-effort, never raises)."""
    if not user_id:
        return
    try:
        message = json.dumps(
            {"event": event_type, "data": data, "timestamp": time.time()}
        )
        await redis_client.publish(f"sse:{user_id}", message)
    except Exception as e:  # noqa: BLE001 - SSE is best-effort, never break dispatch
        logger.debug(f"Failed to publish SSE {event_type}: {e}")


async def speak_on_device(
    redis_client: redis.Redis,
    client_id: str,
    session_id: str,
    text: str,
    timer: Optional[WakeTimer] = None,
) -> None:
    """Synthesize ``text`` and push it to the device via its downlink channel.

    Also opens a short mute window so we don't transcribe our own reply as a
    follow-up. The device can't reach the backend, so the relay serves the audio
    bytes on the LAN. When ``timer`` is given, records the synthesis duration, the
    downlink moment, and the estimated playback length.
    """
    if not client_id or not text:
        return
    _tts_start = time.perf_counter()
    audio = await synthesize_speech(text)
    if timer is not None:
        timer.tts_ms = (time.perf_counter() - _tts_start) * 1000.0
    if not audio:
        return
    # Mute follow-up capture for roughly as long as the reply plays (~2.5 wps).
    play_secs = max(1.5, min(8.0, len(text.split()) * 0.4))
    if timer is not None:
        timer.est_play_secs = play_secs
    await _set_mute(redis_client, session_id, play_secs)
    msg = {
        "type": "play-audio",
        "data": {
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "format": "wav",
        },
    }
    try:
        await redis_client.publish(f"device:downlink:{client_id}", json.dumps(msg))
        if timer is not None:
            timer.mark_downlink()
        logger.info(f"🔊 Sent TTS reply ({len(audio)}B) to device {client_id}")
    except Exception as e:  # noqa: BLE001 - speech output is best-effort
        logger.debug(f"Failed to publish TTS downlink for {client_id}: {e}")


async def execute_voice_command(
    redis_client: redis.Redis,
    plugin_router: PluginRouter,
    *,
    user_id: str,
    session_id: str,
    client_id: str,
    command: str,
    conversation_id: Optional[str] = None,
    source: str = "wake",
    asr_status: str = "transcribed",
    has_speech: bool = True,
    wakeword: Optional[str] = None,
    also_fired: Optional[List[str]] = None,
    score: Optional[float] = None,
    reason: Optional[str] = None,
    capture_secs: Optional[float] = None,
    asr_ms: Optional[float] = None,
    quiet: bool = False,
) -> str:
    """Dispatch a voice command, speak the reply, emit SSE, and manage the window.

    ``source`` is "wake" for acoustic wake commands and "followup" for follow-ups.
    Returns the spoken reply (may be empty). Opens/refreshes the follow-up window
    iff a plugin actually acted (any result success), satisfying the "only follow
    up after an action was done" rule.

    ``capture_secs`` and ``asr_ms`` are pre-dispatch stage durations supplied by
    the acoustic path (the wakeword-service capture window and the batch ASR), so
    the per-command latency line covers the full pipeline. A :class:`WakeTimer`
    traces the rest (per-plugin routing, TTS, downlink) and emits one structured
    log line when the command finishes. When the fast handler (Home Assistant)
    declines and a slower one takes over, a "thinking" tone plays on the device so
    the agentic wait reads as intentional.
    """
    timer = WakeTimer(
        session_id=session_id,
        source=source,
        asr_status=asr_status,
        command=command,
        capture_secs=capture_secs,
        asr_ms=asr_ms,
    )
    # Play the handoff "thinking" tone at most once per command — the instant the
    # first handler declines (should_continue / None result) and another handler
    # is still queued to run.
    handoff_tone_sent = False

    async def _on_plugin_done(
        plugin_id: str, duration_ms: float, result, *, is_last: bool
    ) -> None:
        nonlocal handoff_tone_sent
        timer.record_plugin(plugin_id, duration_ms, result)
        declined = result is None or getattr(result, "should_continue", False)
        if declined and not is_last and not handoff_tone_sent:
            handoff_tone_sent = True
            await play_tone_on_device(redis_client, client_id, "thinking")

    data: dict[str, Any] = {
        "command": command,
        "client_id": client_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "wakeword": wakeword,
        "also_fired": also_fired or [],
        "score": score,
        "reason": reason,
        "asr_status": asr_status,
        "transcript": command,  # alias for plugins that read transcript
        "source": source,
    }

    logger.info(
        f"🎙️ Executing voice command (source={source}, user={user_id}, "
        f"session={session_id}, command='{command[:50]}')"
    )
    # "Thinking" ring while we dispatch — the agent path (e.g. Hermes) can take many
    # seconds, so the wait should read as intentional. Refreshes the end-of-turn cue.
    # Suppressed for quiet sources (e.g. the dial), whose feedback is the result
    # itself (the lights changing) plus the device's own local dial animation.
    if not quiet:
        await set_device_led(
            redis_client,
            client_id,
            effect="Thinking",
            color=_LED_THINK_COLOR,
            duration=15.0,
        )
    try:
        _dispatch_start = time.perf_counter()
        results = await plugin_router.dispatch_event(
            event=PluginEvent.WAKE_WORD_DETECTED,
            user_id=user_id,
            data=data,
            metadata={
                "client_id": client_id,
                "session_id": session_id,
                "conversation_id": conversation_id,
                "command": command,
                "wakeword": wakeword,
                "asr_status": asr_status,
                "has_speech": has_speech,
                "score": score,
                "reason": reason,
                "source": source,
            },
            on_plugin_done=_on_plugin_done,
        )
        timer.dispatch_ms = (time.perf_counter() - _dispatch_start) * 1000.0

        reply = (
            next((r.message for r in results if getattr(r, "message", None)), "") or ""
        )
        acted = any(getattr(r, "success", False) for r in results)

        await publish_sse(
            redis_client,
            user_id,
            "wake.command",
            {
                "command": command,
                "reply": reply,
                "conversation_id": conversation_id,
                "client_id": client_id,
                "asr_status": asr_status,
                "source": source,
            },
        )

        if reply and not quiet:
            await speak_on_device(redis_client, client_id, session_id, reply, timer)

        # Only arm follow-ups when the command actually did something.
        if acted:
            await open_followup_window(redis_client, session_id, command)
            # Tell the UI a follow-up window is open (no wake word needed for the
            # next utterance). window_secs lets the client self-expire the indicator.
            await publish_sse(
                redis_client,
                user_id,
                "wake.followup",
                {
                    "open": True,
                    "window_secs": FOLLOWUP_WINDOW_SECS,
                    "client_id": client_id,
                    "command": command,
                },
            )
            # "Listening" ring for the open follow-up window (no wake word needed).
            if not quiet:
                await set_device_led(
                    redis_client,
                    client_id,
                    effect="Listening For Command",
                    color=_LED_LISTEN_COLOR,
                    duration=float(FOLLOWUP_WINDOW_SECS),
                )

        return reply
    finally:
        timer.log()
