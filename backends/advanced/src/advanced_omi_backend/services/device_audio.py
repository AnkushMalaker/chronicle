"""Stream ``play-audio`` clips to Opus-capable devices (e.g. the Elato firmware).

The generic device downlink sends ``play-audio`` as a single base64 WAV JSON frame.
RAM-limited microcontrollers can't receive that as one large WebSocket frame (the
Elato firmware caps inbound frames at 15 KB), so for Opus-capable clients we transcode
the clip into a stream of small Opus packets — mirroring the Elato server design
(``server/fastapi/esp32_transport.py``): a ``speak-start`` control frame, a sequence of
~60 ms Opus packets sent as binary frames at ~real time, then ``speak-end``.

The device decodes each packet into a ring buffer and plays it gaplessly, so clips of
any length play with bounded memory.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from math import gcd

import numpy as np
import opuslib
import soundfile as sf
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

# Must match the device-side decoder (Audio.cpp: SPK_RATE).
OPUS_RATE = 24000
OPUS_CHANNELS = 1
FRAME_MS = 60  # 60 ms = valid Opus frame at 24 kHz
FRAME_SAMPLES = OPUS_RATE * FRAME_MS // 1000  # 1440 samples / frame
PREROLL_FRAMES = 4  # send a small burst before pacing
_PACE_S = (FRAME_MS / 1000.0) * 0.95  # send slightly ahead of real time


# Newest-wins playback: at most one Opus clip streams to a given device at a time.
# A new clip cancels whatever is still playing so they never interleave on the wire
# (which sounds like two replies "alternating"). Keyed by client_id so it holds even
# across two downlink subscribers (e.g. a stale + a fresh connection for one device).
_active_streams: dict[str, asyncio.Task] = {}


def is_opus_streaming_client(client_id: str) -> bool:
    """True if this client wants streamed Opus audio instead of base64 ``play-audio``.

    For now this is the Elato firmware, identified by its device-name suffix.
    """
    return bool(client_id) and client_id.endswith("-elato")


def _wav_b64_to_pcm24k_mono(audio_b64: str) -> bytes:
    """Decode a base64 WAV into mono 24 kHz signed-16-bit PCM bytes."""
    raw = base64.b64decode(audio_b64)
    data, sr = sf.read(io.BytesIO(raw), dtype="int16", always_2d=True)
    mono = data.mean(axis=1).astype(np.int16) if data.shape[1] > 1 else data[:, 0]

    if sr != OPUS_RATE:
        g = gcd(OPUS_RATE, sr)
        resampled = resample_poly(mono.astype(np.float32), OPUS_RATE // g, sr // g)
        mono = np.clip(np.round(resampled), -32768, 32767).astype(np.int16)

    return mono.tobytes()


def _encode_opus_frames(pcm: bytes) -> list[bytes]:
    """Slice mono 24 kHz PCM into Opus packets (last frame zero-padded)."""
    enc = opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_VOIP)
    frame_bytes = FRAME_SAMPLES * 2  # int16 mono
    packets: list[bytes] = []
    for off in range(0, len(pcm), frame_bytes):
        chunk = pcm[off : off + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        packets.append(enc.encode(chunk, FRAME_SAMPLES))
    return packets


async def _send_opus_stream(websocket, packets: list[bytes]) -> None:
    """Send one clip: ``speak-start`` → paced Opus packets → ``speak-end``.

    If superseded by a newer clip the caller cancels this task; the device drops any
    buffered remainder when it sees the next ``speak-start`` (Audio.cpp flushes its ring).
    """
    await websocket.send_json({"type": "speak-start", "data": {"rate": OPUS_RATE}})
    try:
        for i, pkt in enumerate(packets):
            await websocket.send_bytes(pkt)
            if i >= PREROLL_FRAMES:
                await asyncio.sleep(
                    _PACE_S
                )  # pace ~real time so the device ring stays small
        await websocket.send_json({"type": "speak-end", "data": {}})
    except asyncio.CancelledError:
        # Superseded — stop mid-stream. The newer clip's speak-start flushes the device.
        raise


async def stream_play_audio_as_opus(websocket, data: dict, client_id: str) -> bool:
    """Transcode a ``play-audio`` payload and stream it to the device as Opus.

    Newest-wins: cancels any clip still playing to ``client_id`` so only the latest
    audio is heard. Returns once the new clip is scheduled (it streams in the
    background), so the downlink loop stays free to receive and supersede it.

    Returns True if streamed, False if the payload couldn't be used (caller should
    fall back to forwarding the original message).
    """
    audio_b64 = (data or {}).get("audio_b64")
    if not audio_b64:
        return False

    try:
        pcm = _wav_b64_to_pcm24k_mono(audio_b64)
        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, _encode_opus_frames, pcm)
    except Exception as e:  # noqa: BLE001 - bad audio shouldn't kill the connection
        logger.warning(f"Opus transcode failed, falling back to play-audio: {e}")
        return False

    if not packets:
        return False

    # Drop whatever is still playing to this device before starting the new clip,
    # and wait for it to fully stop so the two never interleave on the same socket.
    prev = _active_streams.pop(client_id, None)
    if prev and not prev.done():
        prev.cancel()
        try:
            await prev
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - prev may fail sending to a dead socket
            pass

    dur_s = len(pcm) / 2 / OPUS_RATE
    logger.info(
        f"🔊 Streaming Opus: {len(packets)} frames (~{dur_s:.1f}s) to {client_id}"
    )

    task = asyncio.create_task(_send_opus_stream(websocket, packets))
    _active_streams[client_id] = task

    def _cleanup(t: asyncio.Task, cid: str = client_id) -> None:
        if _active_streams.get(cid) is t:
            _active_streams.pop(cid, None)

    task.add_done_callback(_cleanup)
    return True


async def stop_play_audio(websocket, client_id: str) -> None:
    """Hard-stop playback on a device (barge-in): cancel the in-flight Opus stream
    and tell the device to flush.

    Cancelling the server-side stream task is essential: the device sets its
    "playing" flag on every Opus packet it decodes, so signalling the firmware
    alone would not stop playback — the next paced packet would resume it. We
    cancel + await the stream task first (no more packets), then send ``speak-stop``
    so the device drops its buffered tail and powers the amp down. Best-effort:
    a dead socket / no active clip is a no-op.
    """
    prev = _active_streams.pop(client_id, None)
    if prev and not prev.done():
        prev.cancel()
        try:
            await prev
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - prev may fail sending to a dead socket
            pass
    try:
        await websocket.send_json({"type": "speak-stop", "data": {}})
        logger.info(f"⏹ Sent speak-stop to {client_id}")
    except Exception as e:  # noqa: BLE001 - device may be gone; nothing to stop
        logger.debug(f"speak-stop to {client_id} failed (device likely gone): {e}")
