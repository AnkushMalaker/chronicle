"""Test the gemma4 /stream WebSocket endpoint end-to-end.

Streams a clip's PCM (16-bit LE, 16 kHz mono) in chunks, prints interim
messages as they arrive, sends CloseStream, and prints the final result —
exactly the Chronicle stt_stream contract.
"""

import argparse
import asyncio
import json

import numpy as np
import soundfile as sf
import websockets

SR = 16000


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://localhost:8767/stream")
    p.add_argument(
        "--audio",
        default="extras/test-audios/audio/Cheap-vs-Expensive-Rug-Tufting-for-the-First-Time-original.wav",
    )
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--chunk-ms", type=int, default=500)
    p.add_argument("--pace", type=float, default=0.05, help="sleep between chunks (s)")
    args = p.parse_args()

    a, sr = sf.read(args.audio, dtype="float32", frames=int(args.seconds * 44100))
    if a.ndim > 1:
        a = a.mean(1)
    if sr != SR:
        import librosa

        a = librosa.resample(a, orig_sr=sr, target_sr=SR)
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
    chunk = int(SR * args.chunk_ms / 1000) * 2
    print(
        f"streaming {len(pcm)/SR/2:.1f}s of audio in {args.chunk_ms}ms chunks",
        flush=True,
    )

    async with websockets.connect(args.url, max_size=None) as ws:
        interims = []

        async def receiver():
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "interim":
                    interims.append(msg["text"])
                    print(f"  [interim] {msg['text'][:120]}", flush=True)
                elif msg["type"] == "final":
                    print(f"\n[FINAL] {msg['text']}", flush=True)
                    print(
                        f"[FINAL] segments={len(msg.get('segments') or [])}", flush=True
                    )
                    return

        recv_task = asyncio.create_task(receiver())
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            await asyncio.sleep(args.pace)
        await ws.send(json.dumps({"type": "CloseStream"}))
        await asyncio.wait_for(recv_task, timeout=180)
        print(f"\n{len(interims)} interim message(s) received", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
