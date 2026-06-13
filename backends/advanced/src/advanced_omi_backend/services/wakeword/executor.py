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
import json
import logging
import os
import time
from typing import Any, List, Optional

import redis.asyncio as redis

from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.plugins.router import PluginRouter
from advanced_omi_backend.services.tts_client import synthesize_speech

logger = logging.getLogger(__name__)

# How long a follow-up window stays open after a successful command (seconds).
FOLLOWUP_WINDOW_SECS = int(os.getenv("FOLLOWUP_WINDOW_SECS", "12"))


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
    redis_client: redis.Redis, client_id: str, session_id: str, text: str
) -> None:
    """Synthesize ``text`` and push it to the device via its downlink channel.

    Also opens a short mute window so we don't transcribe our own reply as a
    follow-up. The device can't reach the backend, so the relay serves the audio
    bytes on the LAN.
    """
    if not client_id or not text:
        return
    audio = await synthesize_speech(text)
    if not audio:
        return
    # Mute follow-up capture for roughly as long as the reply plays (~2.5 wps).
    await _set_mute(
        redis_client, session_id, max(1.5, min(8.0, len(text.split()) * 0.4))
    )
    msg = {
        "type": "play-audio",
        "data": {
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "format": "wav",
        },
    }
    try:
        await redis_client.publish(f"device:downlink:{client_id}", json.dumps(msg))
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
) -> str:
    """Dispatch a voice command, speak the reply, emit SSE, and manage the window.

    ``source`` is "wake" for acoustic wake commands and "followup" for follow-ups.
    Returns the spoken reply (may be empty). Opens/refreshes the follow-up window
    iff a plugin actually acted (any result success), satisfying the "only follow
    up after an action was done" rule.
    """
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
    )

    reply = next((r.message for r in results if getattr(r, "message", None)), "") or ""
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

    if reply:
        await speak_on_device(redis_client, client_id, session_id, reply)

    # Only arm follow-ups when the command actually did something.
    if acted:
        await open_followup_window(redis_client, session_id, command)
        # Tell the UI a follow-up window is open (no wake word needed for the next
        # utterance). window_secs lets the client self-expire the indicator.
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

    return reply
