#!/usr/bin/env python3
"""Probe: can a promptable audio LLM assign speech its ROLE (foreground vs
background), and does conversation context make the difference?

Hypothesis (see background-bucket work): "background" is a role, not an
acoustic property. A clip heard in isolation only supports source-typing
(live human vs played media); the role needs surrounding context. If that is
true, audio LLMs should mislabel deliberately-uploaded media as "background"
when shown the clip alone, and recover the correct role given a 60s window.

Eval classes (ground truth from human review):
  bg_media  — confirmed background-speech clips (media playing behind a live
              conversation, plus one live-but-background traffic voice)
  fg_media  — clips from deliberate media uploads (the media IS the subject)
  fg_live   — confirmed live foreground speech (not_background reviews)

Conditions: isolated (exact clip) vs context (60s window, target span marked).
Models: gpt-audio-1.5 (OpenAI) and gemini-3.1-pro-preview (Google).

Paid responses are cached in Mongo chronicle.audio_llm_response_cache keyed by
audio sha256 + model + prompt sha256 (repo convention for paid APIs).

Run:
    uv run --with openai --with google-genai --with pymongo \
        --with python-dotenv --with requests \
        python scripts/media_role_probe.py [--limit-per-class N] [--dry-run]
"""

import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent.parent
BACKEND_ENV = dotenv_values(BACKEND_DIR / ".env")
ML_ENV = dotenv_values(REPO_ROOT / "extras" / "ml-experiments" / ".env")

API = "http://localhost:8000"
MONGO = MongoClient("mongodb://localhost:27017")
DB = MONGO["chronicle"]
CACHE = DB["audio_llm_response_cache"]

OPENAI_MODEL = "gpt-audio-1.5"
GEMINI_MODEL = "gemini-3.1-pro-preview"
CONTEXT_SECONDS = 60.0

# Role ground truth needs curation the DB cannot provide: bucket clips carry
# SOURCE labels (media vs live), not roles, and "upload" alone does not imply
# the content is media (several uploads are recorded live meetings).
# Media-content uploads — deliberately ingested videos, so media = foreground:
MEDIA_UPLOAD_CONVS = {
    "475bdd03-c3ea-4f34-b76e-b4dc0f8af8d6",  # glass-blowing video
    "e443b615-5dc3-4021-89b2-4a6f8c186006",  # "kitchen sink mix" glass video
    "ffbd432e-5fd9-4a1c-93d5-be22899e9d9d",  # drug-testing-videos commentary
}


def _is_upload(conversation_id: str) -> bool:
    doc = DB["conversations"].find_one(
        {"conversation_id": conversation_id}, {"client_id": 1, "title": 1}
    )
    if not doc:
        return False
    return "speaker-mining" in (doc.get("client_id") or "") or "upload" in (
        doc.get("title") or ""
    )

ROLE_QUESTION = """\
Decide the ROLE of the target speech in this recording from an always-on \
personal recorder:
- "foreground": the primary content — the wearer's own conversation, or media \
the wearer is deliberately capturing/analyzing as the subject of the recording.
- "background": audio that merely happens to be present behind the primary \
activity (a TV/video/music playing while people talk, or bystanders).
Also classify the SOURCE of the target speech:
- "live_human": a person speaking live in the room/scene.
- "played_media": produced/played-back audio (TV, video, podcast, music).
Respond with a single JSON object, no markdown fences:
{"role": "foreground" or "background", "source_type": "live_human" or \
"played_media", "confidence": <0..1>, "reason": "<one short sentence>"}\
"""

ISOLATED_PROMPT = (
    "Listen to this short audio clip. The target speech is the entire clip.\n"
    + ROLE_QUESTION
)

CONTEXT_HEADER = (
    "This is a ~{window:.0f} second continuous excerpt from a longer "
    "recording. The target speech is ONLY the span between {t1:.1f}s and "
    "{t2:.1f}s in this excerpt. Use the surrounding context — who talks "
    "before and after, whether people converse with each other, whether "
    "media plays continuously — to judge the target span.\n"
)


def login() -> str:
    resp = requests.post(
        f"{API}/auth/jwt/login",
        data={
            "username": BACKEND_ENV["ADMIN_EMAIL"],
            "password": BACKEND_ENV["ADMIN_PASSWORD"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_wav(token: str, conversation_id: str, start: float, duration: float) -> bytes:
    resp = requests.get(
        f"{API}/api/conversations/{conversation_id}/audio-segments",
        params={"start": round(start, 3), "duration": round(duration, 3), "format": "wav"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def build_manifest(limit_per_class: int) -> list[dict]:
    """Assemble labelled clips: role ground truth from human review decisions."""
    corpus = DB["background_corpus_embeddings"]
    clips: list[dict] = []

    def corpus_row(conversation_id: str, start: float) -> dict | None:
        return corpus.find_one(
            {
                "conversation_id": conversation_id,
                "start": {"$gte": start - 0.05, "$lte": start + 0.05},
            },
            {"_id": 0, "embedding": 0},
        )

    # bg_media: confirmed background-speech bucket clips from LIVE captures
    # only — media clips confirmed inside uploads are source-labels, not roles
    seen_convs: dict[str, int] = {}
    upload_cache: dict[str, bool] = {}
    for doc in DB["background_clips"].find(
        {"bucket_type": "background_speech"},
        {"conversation_id": 1, "segment_start": 1, "segment_end": 1},
    ).sort("segment_start", 1):
        conv = doc["conversation_id"]
        if conv not in upload_cache:
            upload_cache[conv] = _is_upload(conv)
        if upload_cache[conv]:
            continue
        if seen_convs.get(conv, 0) >= max(5, limit_per_class // 2):
            continue
        row = corpus_row(conv, float(doc["segment_start"])) or {}
        clips.append(
            {
                "label_role": "background",
                "label_class": "bg_media",
                "conversation_id": conv,
                "start": float(doc["segment_start"]),
                "end": float(doc["segment_end"]),
                "text": row.get("text"),
                "conversation_title": row.get("conversation_title"),
            }
        )
        seen_convs[conv] = seen_convs.get(conv, 0) + 1
    bg = [c for c in clips if c["label_class"] == "bg_media"][:limit_per_class]

    # fg_media: clips from curated media-content uploads — media is the subject
    fg_media: list[dict] = []
    per_conv = max(2, limit_per_class // len(MEDIA_UPLOAD_CONVS))
    for conv_id in sorted(MEDIA_UPLOAD_CONVS):
        rows = list(
            corpus.find(
                {
                    "conversation_id": conv_id,
                    "candidate_type": "background_speech",
                    "text": {"$nin": [None, ""]},
                },
                {"_id": 0, "embedding": 0},
            ).sort("start", 1)
        )
        step = max(1, len(rows) // per_conv)
        for row in rows[::step][:per_conv]:
            fg_media.append(
                {
                    "label_role": "foreground",
                    "label_class": "fg_media",
                    "conversation_id": row["conversation_id"],
                    "start": float(row["start"]),
                    "end": float(row["end"]),
                    "text": row.get("text"),
                    "conversation_title": row.get("conversation_title"),
                }
            )
    fg_media = fg_media[:limit_per_class]

    # fg_live: confirmed live foreground speech, one clip per conversation
    fg_live: list[dict] = []
    used: set[str] = set()
    for doc in DB["background_foreground_clips"].find(
        {}, {"clip_key": 1, "conversation_id": 1}
    ):
        conv = doc.get("conversation_id")
        key = doc.get("clip_key") or ""
        parts = key.split(":")
        if not conv or conv in used or len(parts) < 3:
            continue
        if conv not in upload_cache:
            upload_cache[conv] = _is_upload(conv)
        if upload_cache[conv]:
            continue
        try:
            start, end = float(parts[1]), float(parts[2])
        except ValueError:
            continue
        row = corpus_row(conv, start) or {}
        fg_live.append(
            {
                "label_role": "foreground",
                "label_class": "fg_live",
                "conversation_id": conv,
                "start": start,
                "end": end,
                "text": row.get("text"),
                "conversation_title": row.get("conversation_title"),
            }
        )
        used.add(conv)
        if len(fg_live) >= limit_per_class:
            break

    return bg + fg_media + fg_live


def parse_json(raw: str) -> dict:
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    if not t.strip().startswith("{"):
        s, e = t.find("{"), t.rfind("}")
        t = t[s : e + 1]
    return json.loads(t)


def cached_call(model: str, prompt: str, wav: bytes, call) -> tuple[str, bool]:
    """Content-hash cache for paid audio-LLM calls (repo convention)."""
    key = {
        "audio_sha256": hashlib.sha256(wav).hexdigest(),
        "model": model,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    hit = CACHE.find_one(key)
    if hit:
        return hit["response_text"], True
    text = call()
    CACHE.update_one(
        key,
        {
            "$set": {
                "response_text": text,
                "prompt": prompt,
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return text, False


def call_openai(client, prompt: str, wav: bytes) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        modalities=["text"],
        temperature=0.2,
        max_completion_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(wav).decode(),
                            "format": "wav",
                        },
                    },
                ],
            }
        ],
    )
    return resp.choices[0].message.content or ""


def call_gemini(client, prompt: str, wav: bytes) -> str:
    from google.genai import types

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            prompt,
            types.Part.from_bytes(data=wav, mime_type="audio/wav"),
        ],
        config=types.GenerateContentConfig(temperature=0.2),
    )
    return resp.text or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-per-class", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="build manifest only")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=CONTEXT_SECONDS,
        help="context window size for the context condition",
    )
    parser.add_argument(
        "--classes",
        default="bg_media,fg_media,fg_live",
        help="comma-separated label classes to run",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=BACKEND_DIR / "data" / "media_role_probe" / "results.jsonl",
    )
    args = parser.parse_args()

    wanted = set(args.classes.split(","))
    window = args.window_seconds
    manifest = [
        clip
        for clip in build_manifest(args.limit_per_class)
        if clip["label_class"] in wanted
    ]
    counts: dict[str, int] = {}
    for clip in manifest:
        counts[clip["label_class"]] = counts.get(clip["label_class"], 0) + 1
    print(f"manifest: {len(manifest)} clips {counts}", flush=True)
    if args.dry_run:
        for clip in manifest:
            print(
                f"  {clip['label_class']:8s} {clip['conversation_id'][:8]} "
                f"{clip['start']:9.2f}-{clip['end']:.2f} :: "
                f"{(clip['text'] or '')[:60]}"
            )
        return 0

    from openai import OpenAI
    from google import genai

    openai_client = OpenAI(api_key=BACKEND_ENV["OPENAI_API_KEY"])
    gemini_client = genai.Client(api_key=ML_ENV["GOOGLE_API_KEY"])
    token = login()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    audio_dir = args.out.parent / "audio"
    audio_dir.mkdir(exist_ok=True)

    for n, clip in enumerate(manifest, 1):
        conv, start, end = clip["conversation_id"], clip["start"], clip["end"]
        window_start = max(0.0, start - window / 2)
        conditions = {
            "isolated": (start, max(end - start, 0.5), ISOLATED_PROMPT),
            "context": (
                window_start,
                window,
                CONTEXT_HEADER.format(
                    window=window,
                    t1=start - window_start,
                    t2=end - window_start,
                )
                + ROLE_QUESTION,
            ),
        }
        for condition, (fetch_start, duration, prompt) in conditions.items():
            wav_path = audio_dir / f"{conv[:8]}_{fetch_start:.2f}_{duration:.2f}.wav"
            if wav_path.exists():
                wav = wav_path.read_bytes()
            else:
                wav = fetch_wav(token, conv, fetch_start, duration)
                wav_path.write_bytes(wav)
            for model, caller in (
                (OPENAI_MODEL, lambda p=prompt, w=wav: call_openai(openai_client, p, w)),
                (GEMINI_MODEL, lambda p=prompt, w=wav: call_gemini(gemini_client, p, w)),
            ):
                try:
                    raw, was_cached = cached_call(model, prompt, wav, caller)
                    parsed = parse_json(raw)
                except Exception as exc:
                    print(f"[{n}] {model} {condition} ERROR: {exc}", flush=True)
                    continue
                record = {
                    **{k: clip[k] for k in ("label_role", "label_class", "conversation_id", "start", "end", "text")},
                    "condition": condition,
                    "model": model,
                    "predicted_role": parsed.get("role"),
                    "predicted_source": parsed.get("source_type"),
                    "confidence": parsed.get("confidence"),
                    "reason": parsed.get("reason"),
                    "cached": was_cached,
                }
                results.append(record)
                if not was_cached:
                    time.sleep(1)  # stay under per-minute rate limits
        print(
            f"[{n}/{len(manifest)}] {clip['label_class']} {conv[:8]} done",
            flush=True,
        )

    with args.out.open("w", encoding="utf-8") as fh:
        for record in results:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print("\n=== role accuracy (predicted_role vs label_role) ===")
    for model in (OPENAI_MODEL, GEMINI_MODEL):
        for condition in ("isolated", "context"):
            rows = [
                r
                for r in results
                if r["model"] == model and r["condition"] == condition
            ]
            if not rows:
                continue
            print(f"\n{model} / {condition}:")
            for klass in ("bg_media", "fg_media", "fg_live"):
                sub = [r for r in rows if r["label_class"] == klass]
                if not sub:
                    continue
                correct = sum(r["predicted_role"] == r["label_role"] for r in sub)
                print(f"  {klass:8s} {correct}/{len(sub)}")
            correct = sum(r["predicted_role"] == r["label_role"] for r in rows)
            print(f"  overall  {correct}/{len(rows)}")
    print(f"\nresults: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
