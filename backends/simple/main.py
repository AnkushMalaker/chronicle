#!/usr/bin/env python3
"""Simple WebSocket Audio Service

* Accepts Opus packets over a WebSocket (`/ws`).
* Uses OmiSDK (`OmiOpusDecoder`) to convert them to 16-kHz/16-bit/mono PCM.
* Buffers PCM and writes 30-second WAV chunks to `./audio_chunks/`.
"""

from __future__ import annotations

import json
import logging
import os
import wave
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from omi.decoder import OmiOpusDecoder  # OmiSDK

###############################################################################
# Configuration
###############################################################################
SAMPLE_RATE = 16_000  # Hz
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes (16‑bit)
SEGMENT_SECONDS = 30  # length of each stored chunk
TARGET_SAMPLES = SAMPLE_RATE * SEGMENT_SECONDS

# Directory where WAV chunks are written
CHUNK_DIR = Path("./audio_chunks")
CHUNK_DIR.mkdir(parents=True, exist_ok=True)


###############################################################################
# Wyoming Protocol Support
###############################################################################
async def parse_wyoming_protocol(ws: WebSocket) -> Tuple[dict, Optional[bytes]]:
    """Parse Wyoming protocol or fall back to raw Opus audio.

    Returns:
        Tuple of (header_dict, payload_bytes or None)
    """
    # Read data from WebSocket
    message = await ws.receive()

    # Handle text message (JSON header)
    if "text" in message:
        header_text = message["text"]
        # Wyoming protocol uses newline-terminated JSON
        if not header_text.endswith("\n"):
            header_text += "\n"

        # Parse JSON header
        json_line = header_text.strip()
        try:
            header = json.loads(json_line)
        except json.JSONDecodeError:
            # Invalid JSON, treat as error
            audio_logger.warning(f"Invalid JSON header: {json_line}")
            return {"type": "error", "data": {"message": "Invalid JSON"}}, None

        # If payload is expected, read binary data
        payload = None
        if header.get("payload_length", 0) > 0:
            payload_msg = await ws.receive()
            if "bytes" in payload_msg:
                payload = payload_msg["bytes"]
            else:
                audio_logger.warning(
                    f"Expected binary payload but got: {payload_msg.keys()}"
                )

        return header, payload

    # Handle binary message (backward compatibility - treat as raw Opus audio)
    elif "bytes" in message:
        # For simple backend, we expect Opus audio, not PCM
        opus_data = message["bytes"]
        # Create a synthetic audio-chunk header
        header = {
            "type": "audio-chunk",
            "data": {
                "format": "opus",  # Indicate this is Opus, not PCM
                "timestamp": int(datetime.utcnow().timestamp() * 1000),
            },
            "data_length": None,
            "payload_length": len(opus_data),
        }
        return header, opus_data

    else:
        raise ValueError(f"Unexpected WebSocket message type: {message.keys()}")


###############################################################################
# FastAPI WebSocket server
###############################################################################
app = FastAPI(title="Simple Audio Service")

audio_logger = logging.getLogger("audio_service")
audio_logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


class ClientState:
    """Keep per-connection state."""

    def __init__(self, ws: WebSocket, client_id: int):
        self.ws = ws
        self.id = client_id
        self.pcm_frames: List[bytes] = []  # raw PCM chunks
        self.sample_count = 0  # samples accumulated in current segment
        self.segment_index = 0
        self.start_time = datetime.utcnow().isoformat()
        self.decoder = OmiOpusDecoder()

    # --------------------------------------------------------------------- #
    async def add_opus_packet(self, packet: bytes):
        """Decode a single Opus packet -> PCM, buffer it, handle rollover."""
        pcm = self.decoder.decode_packet(packet, strip_header=False)
        if pcm is None:
            audio_logger.error("Client %s: decode failed (packet dropped)", self.id)
            return
        audio_logger.debug("Client %s: decoded packet", self.id)

        self.pcm_frames.append(pcm)
        self.sample_count += len(pcm) // SAMPLE_WIDTH  # samples per frame

        if self.sample_count >= TARGET_SAMPLES:
            await self._flush_segment()

    # ------------------------------------------------------------------ #
    async def _flush_segment(self):
        if not self.pcm_frames:
            return

        pcm_bytes = b"".join(self.pcm_frames)
        duration = self.sample_count / SAMPLE_RATE

        # Write WAV to disk ------------------------------------------------ #
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        wav_path = CHUNK_DIR / f"client{self.id}_{timestamp}_{self.segment_index}.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_bytes)
        audio_logger.info(
            "Client %s: wrote %s (%.1fs)", self.id, wav_path.name, duration
        )

        # Reset buffers
        self.pcm_frames.clear()
        self.sample_count = 0
        self.segment_index += 1


# ------------------------------------------------------------------------- #
# WebSocket endpoint
# ------------------------------------------------------------------------- #

clients: List[ClientState] = []


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Accept handshake
    await ws.accept()
    client_id = len(clients) + 1
    state = ClientState(ws, client_id)
    clients.append(state)
    audio_logger.info("Client %s connected", client_id)

    audio_session_active = False
    packet_count = 0

    try:
        while True:
            try:
                audio_logger.debug("Client %s: waiting for message...", client_id)
                # Parse Wyoming protocol or raw audio
                header, payload = await parse_wyoming_protocol(ws)

                if header["type"] == "audio-start":
                    # Audio session started
                    audio_session_active = True
                    audio_format = header.get("data", {})
                    audio_logger.info(
                        f"Client {client_id}: Audio session started - "
                        f"Format: {audio_format}"
                    )
                    packet_count = 0

                elif header["type"] == "audio-chunk" and payload:
                    packet_count += 1
                    # Check if this is Opus audio (from Wyoming or raw)
                    if header.get("data", {}).get(
                        "format"
                    ) == "opus" or not "format" in header.get("data", {}):
                        # This is Opus audio that needs decoding
                        audio_logger.debug(
                            f"Client {client_id}: received Opus packet #{packet_count} "
                            f"(size: {len(payload)} bytes)"
                        )
                        await state.add_opus_packet(payload)
                    else:
                        # Non-Opus audio format, log and skip
                        audio_logger.warning(
                            f"Client {client_id}: Received non-Opus audio format: "
                            f"{header.get('data', {}).get('format')}"
                        )

                elif header["type"] == "audio-stop":
                    # Audio session stopped
                    audio_session_active = False
                    audio_logger.info(
                        f"Client {client_id}: Audio session stopped - "
                        f"Total packets: {packet_count}"
                    )
                    # Flush any remaining audio
                    await state._flush_segment()
                    packet_count = 0

                elif header["type"] == "error":
                    # Error in protocol parsing
                    audio_logger.error(
                        f"Client {client_id}: Protocol error - {header.get('data', {}).get('message')}"
                    )

                else:
                    # Unknown event type, ignore
                    audio_logger.debug(
                        f"Client {client_id}: Ignoring Wyoming event type '{header['type']}'"
                    )

            except WebSocketDisconnect:
                break  # graceful client close
            except Exception as e:
                audio_logger.error(
                    f"Client {client_id}: Error processing message - {e}"
                )

    finally:
        clients.remove(state)
        # Flush remainder (<30s) before goodbye
        await state._flush_segment()
        audio_logger.info("Client %s disconnected", client_id)


###############################################################################
# Entrypoint
###############################################################################
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    audio_logger.info("Starting simple audio service at ws://%s:%s/ws", host, port)
    uvicorn.run("main:app", host=host, port=port, reload=False)
