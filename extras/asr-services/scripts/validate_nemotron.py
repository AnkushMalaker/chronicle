"""Validate the Nemotron streaming ASR service on a WAV file.

Exercises both endpoints the Chronicle backend uses:
  * POST /transcribe  — offline full-file decode
  * WS   /stream      — cache-aware streaming (binary PCM in, interim/final out),
                        driven exactly like backend RegistryStreamingProvider:
                        raw PCM frames, then {"type":"CloseStream"} to finalize.

Usage:
    uv run python scripts/validate_nemotron.py <wav_path> [--port 8771]
"""

import argparse
import asyncio
import json
import sys
import time
import wave

import httpx
import websockets

CHUNK_SECONDS = 0.25  # mirrors AudioStreamProducer's 0.25s chunks


def read_pcm(path: str):
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1, "expected mono"
        assert wf.getsampwidth() == 2, "expected 16-bit"
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, rate


async def test_offline(base_url: str, path: str):
    print(f"\n=== /transcribe (offline) ===")
    t0 = time.time()
    with open(path, "rb") as f:
        files = {"file": ("audio.wav", f, "audio/wav")}
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{base_url}/transcribe", files=files)
    resp.raise_for_status()
    data = resp.json()
    dt = time.time() - t0
    print(f"  time: {dt:.1f}s")
    print(f"  words: {len(data.get('words') or [])}")
    print(f"  text: {data.get('text', '')!r}")
    return data.get("text", "")


async def test_streaming(ws_url: str, pcm: bytes, rate: int):
    print(f"\n=== /stream (cache-aware streaming) ===")
    chunk_bytes = int(rate * 2 * CHUNK_SECONDS)
    interims = []
    final = None
    t0 = time.time()
    first_interim_at = None
    # ping_interval=None: a client that continuously pushes audio doesn't rely on
    # library-level keepalive (GIL-heavy RNNT decode can starve the server loop
    # past a 20s ping window while it drains its backlog after CloseStream).
    async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:

        async def sender():
            for i in range(0, len(pcm), chunk_bytes):
                await ws.send(pcm[i : i + chunk_bytes])
                await asyncio.sleep(CHUNK_SECONDS)  # real-time pacing
            await ws.send(json.dumps({"type": "CloseStream"}))

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=120)
                data = json.loads(msg)
                if data.get("type") == "interim":
                    if first_interim_at is None:
                        first_interim_at = time.time() - t0
                    interims.append(data.get("text", ""))
                elif data.get("type") == "final":
                    final = data
                    break
        finally:
            send_task.cancel()
    dt = time.time() - t0
    print(f"  interims received: {len(interims)}")
    if first_interim_at is not None:
        print(f"  first interim at: {first_interim_at:.2f}s into stream")
    if interims:
        print(f"  sample interims:")
        for t in interims[: min(3, len(interims))]:
            print(f"    - {t!r}")
        print(f"    - (last) {interims[-1]!r}")
    print(f"  final words: {len(final.get('words') or []) if final else 0}")
    print(f"  final text: {final.get('text', '') if final else None!r}")
    print(f"  total stream time: {dt:.1f}s")
    return (final or {}).get("text", ""), interims


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--host", default="localhost")
    args = ap.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    ws_url = f"ws://{args.host}:{args.port}/stream"

    # Wait for health
    print(f"Waiting for {base_url}/health ...")
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(120):
            try:
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200 and r.json().get("status") == "healthy":
                    print(f"  healthy: {r.json()}")
                    break
            except Exception:
                pass
            await asyncio.sleep(5)
        else:
            print("Service did not become healthy in time")
            sys.exit(1)

    pcm, rate = read_pcm(args.wav)
    print(f"audio: {len(pcm)} bytes @ {rate}Hz ({len(pcm)/(rate*2):.1f}s)")

    offline_text = await test_offline(base_url, args.wav)
    stream_text, interims = await test_streaming(ws_url, pcm, rate)

    print("\n=== SUMMARY ===")
    print(f"streaming produced {len(interims)} interim updates")
    print(f"offline   final text len: {len(offline_text)}")
    print(f"streaming final text len: {len(stream_text)}")
    ok = bool(stream_text.strip()) and len(interims) > 1
    print(
        f"RESULT: {'PASS' if ok else 'FAIL'} "
        f"(needs non-empty streaming final + multiple interims)"
    )
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    asyncio.run(main())
