"""
Qwen3-ASR Streaming Bridge.

WebSocket server that accepts audio chunks from Chronicle's
RegistryStreamingTranscriptionProvider and relays them to a vLLM
server running Qwen3-ASR via its OpenAI-compatible API with SSE streaming.

Since Qwen3-ASR (via vLLM) does not support incremental audio input like
Deepgram's WebSocket, this bridge accumulates audio in a buffer and
periodically sends it to vLLM for transcription. It emits interim results
as the buffer grows and a final result when the stream ends.

Protocol (WebSocket messages from Chronicle):
    - Binary frames: raw PCM audio data (16-bit LE, 16 kHz mono)
    - JSON {"type": "CloseStream"}: end of audio session

Protocol (WebSocket messages to Chronicle — matching config.yml expect):
    - JSON {"type": "interim", "text": "..."}: interim transcription
    - JSON {"type": "final", "text": "...", "segments": [...]}:  final result

Environment variables:
    QWEN3_VLLM_URL: vLLM server URL (default: http://localhost:8000)
    ASR_MODEL: Model identifier (default: Qwen/Qwen3-ASR-1.7B)
    STREAM_INTERVAL_SECONDS: Seconds of audio to accumulate before
        sending an interim request (default: 3)
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import wave
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

VLLM_URL = os.getenv("QWEN3_VLLM_URL", "http://localhost:8000").rstrip("/")
ASR_MODEL = os.getenv("ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
STREAM_INTERVAL = float(os.getenv("STREAM_INTERVAL_SECONDS", "3"))
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1

app = FastAPI(title="Qwen3-ASR Streaming Bridge", version="1.0.0")


def _pcm_to_wav_base64(pcm_data: bytes, sample_rate: int = SAMPLE_RATE) -> str:
    """Wrap raw PCM bytes in a WAV container and return base64."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_qwen3_output(raw: str) -> str:
    """Parse Qwen3-ASR raw output, stripping the 'language X<asr_text>' wrapper.

    Returns clean transcript text only.
    """
    if not raw:
        return ""

    s = raw.strip()
    if not s:
        return ""

    tag = "<asr_text>"
    if tag not in s:
        return s

    meta_part, text_part = s.split(tag, 1)

    # Strip closing tag if present
    end_tag = "</asr_text>"
    if text_part.endswith(end_tag):
        text_part = text_part[: -len(end_tag)]

    # Silent audio ("language None") → empty
    if "language none" in meta_part.lower():
        t = text_part.strip()
        if not t:
            return ""
        return t

    return text_part.strip()


async def _transcribe_vllm(
    audio_b64: str, client: httpx.AsyncClient, stream: bool = False
) -> str:
    """Send audio to vLLM and return the transcription text.

    Args:
        audio_b64: Base64-encoded WAV audio.
        client: Shared httpx client.
        stream: If True, use SSE streaming and return text as it arrives.

    Returns:
        Transcription text string.
    """
    audio_url = f"data:audio/wav;base64,{audio_b64}"

    payload = {
        "model": ASR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "audio_url", "audio_url": {"url": audio_url}},
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
        "stream": stream,
    }

    if stream:
        text_parts: list[str] = []
        async with client.stream(
            "POST", f"{VLLM_URL}/v1/chat/completions", json=payload, timeout=60.0
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        text_parts.append(content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        return _parse_qwen3_output("".join(text_parts))
    else:
        resp = await client.post(
            f"{VLLM_URL}/v1/chat/completions", json=payload, timeout=60.0
        )
        resp.raise_for_status()
        data = resp.json()
        return _parse_qwen3_output(data["choices"][0]["message"]["content"])


@app.websocket("/")
async def websocket_stream(ws: WebSocket):
    """Handle a streaming transcription session.

    The Chronicle streaming consumer connects here, sends binary PCM chunks,
    and expects JSON interim/final messages back.
    """
    await ws.accept()
    logger.info("Streaming bridge: new connection")

    audio_buffer = bytearray()
    prev_transcript = ""
    running = True

    async with httpx.AsyncClient() as client:
        try:
            while running:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=30.0)
                except asyncio.TimeoutError:
                    # No data for 30s — send keepalive or close
                    continue

                if "bytes" in message:
                    audio_buffer.extend(message["bytes"])

                    # Check if we have enough audio for an interim transcription
                    buffer_duration = len(audio_buffer) / (
                        SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
                    )
                    if buffer_duration >= STREAM_INTERVAL:
                        try:
                            wav_b64 = _pcm_to_wav_base64(bytes(audio_buffer))
                            text = await _transcribe_vllm(wav_b64, client, stream=True)
                            text = text.strip()

                            if text and text != prev_transcript:
                                await ws.send_json(
                                    {
                                        "type": "interim",
                                        "text": text,
                                        "words": [],
                                        "segments": [],
                                    }
                                )
                                prev_transcript = text
                        except Exception as e:
                            logger.warning(f"Interim transcription failed: {e}")

                elif "text" in message:
                    try:
                        msg = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type", "")

                    if msg_type in ("CloseStream", "stop"):
                        running = False

                elif message.get("type") == "websocket.disconnect":
                    running = False

            # --- Final transcription on stream end ---
            if audio_buffer:
                try:
                    wav_b64 = _pcm_to_wav_base64(bytes(audio_buffer))
                    text = await _transcribe_vllm(wav_b64, client, stream=False)
                    text = text.strip()

                    await ws.send_json(
                        {
                            "type": "final",
                            "text": text,
                            "words": [],
                            "segments": [],
                        }
                    )
                    logger.info(f"Final transcript: {len(text)} chars")
                except Exception as e:
                    logger.error(f"Final transcription failed: {e}")
                    await ws.send_json(
                        {
                            "type": "final",
                            "text": prev_transcript,
                            "words": [],
                            "segments": [],
                        }
                    )
            else:
                await ws.send_json(
                    {
                        "type": "final",
                        "text": "",
                        "words": [],
                        "segments": [],
                    }
                )

        except WebSocketDisconnect:
            logger.info("Streaming bridge: client disconnected")
        except Exception as e:
            logger.error(f"Streaming bridge error: {e}", exc_info=True)
        finally:
            logger.info(
                f"Streaming bridge: session ended, processed {len(audio_buffer)} bytes "
                f"({len(audio_buffer) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS):.1f}s audio)"
            )


@app.get("/health")
async def health():
    """Health check endpoint."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{VLLM_URL}/health", timeout=5.0)
            vllm_ok = resp.status_code == 200
        except Exception:
            vllm_ok = False

    return {
        "status": "healthy" if vllm_ok else "degraded",
        "vllm_url": VLLM_URL,
        "vllm_reachable": vllm_ok,
        "model": ASR_MODEL,
        "stream_interval_seconds": STREAM_INTERVAL,
    }


def main():
    parser = argparse.ArgumentParser(description="Qwen3-ASR Streaming Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8766, help="Port to bind to")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
