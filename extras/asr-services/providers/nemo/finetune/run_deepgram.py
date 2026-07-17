"""Get word-level timestamps for CoSHE clips from Deepgram Nova-3 (multilingual),
emitting the normalized JSONL that segment_by_alignment.py consumes:

    {"audio_file_name": "...", "dg_text": "<deepgram hypothesis>",
     "words": [{"word": "...", "start": 1.2, "end": 1.4}, ...]}

Deepgram Nova-3 multilingual is the only option that advertises Hindi<->English
intra-utterance code-switching (CoSHE is exactly that) and returns precise word
timestamps. Already the project's transcription provider, so the key is in hand.
~$0.0092/min -> ~$16 for all 1985 clips (likely free under the $200 credit).

Start small to de-risk alignment quality on real Hinglish before spending on all 1985:
    DEEPGRAM_API_KEY=... python run_deepgram.py --manifest all.json \
        --out hyp_words.jsonl --limit 5

Then the full run (drop --limit). Resumable: clips already in --out are skipped.

`dg_text` is kept so we also get a free Deepgram-vs-base WER on CoSHE later.
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests

DG_URL = "https://api.deepgram.com/v1/listen"


def transcribe(wav_path: str, key: str, model: str, language: str) -> dict:
    """POST one WAV to Deepgram prerecorded; return its first alternative dict."""
    params = {
        "model": model,
        "language": language,
        "punctuate": "true",
        "smart_format": "false",
    }
    with open(wav_path, "rb") as f:
        resp = requests.post(
            DG_URL,
            params=params,
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            data=f.read(),
            timeout=120,
        )
    resp.raise_for_status()
    alts = resp.json()["results"]["channels"][0]["alternatives"]
    return alts[0] if alts else {"transcript": "", "words": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manifest", required=True, help="all.json (audio_filepath + audio_file_name)"
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="nova-3")
    ap.add_argument(
        "--language", default="multi", help="'multi' = Nova-3 code-switching"
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="only the first N clips (validation)"
    )
    ap.add_argument("--names", nargs="+", help="only these audio_file_name values")
    args = ap.parse_args()

    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise SystemExit("set DEEPGRAM_API_KEY")

    rows = [json.loads(l) for l in open(args.manifest)]
    if args.names:
        want = set(args.names)
        rows = [r for r in rows if r["audio_file_name"] in want]
    if args.limit:
        rows = rows[: args.limit]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        for l in open(out_path):
            try:
                done.add(json.loads(l)["audio_file_name"])
            except Exception:
                pass
    todo = [r for r in rows if r["audio_file_name"] not in done]
    print(
        f"{len(done)} done, {len(todo)} to transcribe (model={args.model} lang={args.language})",
        flush=True,
    )

    t0 = time.time()
    with open(out_path, "a") as out_f:
        for i, r in enumerate(todo, 1):
            try:
                alt = transcribe(r["audio_filepath"], key, args.model, args.language)
            except Exception as e:
                print(f"  ERR {r['audio_file_name']}: {str(e)[:160]}", flush=True)
                continue
            words = [
                {"word": w["word"], "start": w["start"], "end": w["end"]}
                for w in alt.get("words", [])
            ]
            out_f.write(
                json.dumps(
                    {
                        "audio_file_name": r["audio_file_name"],
                        "dg_text": alt.get("transcript", ""),
                        "words": words,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out_f.flush()
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  ({time.time()-t0:.0f}s)", flush=True)
    print(f"DONE -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
