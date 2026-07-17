"""Send an audio window to Gemini for a reference transcription."""

import argparse
import base64
import io
import json
import os
import urllib.error
import urllib.request

import soundfile as sf

SR = 16000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio", default="/w/ankush-yap-fixed.wav")
    p.add_argument("--offset", type=float, default=0.0)
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--model", default="gemini-3.5-flash")
    args = p.parse_args()

    a, sr = sf.read(args.audio, dtype="float32")
    if a.ndim > 1:
        a = a.mean(1)
    if sr != SR:
        import librosa

        a = librosa.resample(a, orig_sr=sr, target_sr=SR)
    seg = a[int(args.offset * SR) : int((args.offset + args.seconds) * SR)]
    buf = io.BytesIO()
    sf.write(buf, seg, SR, format="WAV", subtype="PCM_16")
    b64 = base64.b64encode(buf.getvalue()).decode()
    print(
        f"window {args.offset:.0f}-{args.offset+args.seconds:.0f}s, {len(b64)/1e6:.1f}MB b64",
        flush=True,
    )

    key = os.environ["GOOGLE_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent?key={key}"
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Transcribe this audio verbatim. Output only the transcript text."
                    },
                    {"inline_data": {"mime_type": "audio/wav", "data": b64}},
                ]
            }
        ]
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=120))
        cand = resp["candidates"][0]["content"]["parts"][0]["text"]
        print("=== GEMINI TRANSCRIPT ===", flush=True)
        print(cand, flush=True)
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:800], flush=True)


if __name__ == "__main__":
    main()
