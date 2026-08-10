#!/usr/bin/env python3
"""Sample audio where VAD and ASR disagree, so the disagreement can be judged by ear.

VAD reporting speech is not evidence of a fault. A stream, a film, or a podcast running
all night *is* speech, and 87% speech across sixteen hours may be exactly right. The only
claim worth testing is the narrower one: VAD says speech here, and the transcriber
produced no words. That has several possible causes — over-triggering on noise, audio too
distant or degraded to transcribe, or a transcription that simply failed — and they are
not distinguishable from the numbers.

So this extracts the clips and asks a human. Windows where the two agree are sampled too,
as controls: a page showing only failures makes everything look broken.

Clips are embedded as base64 WAV, so the page works offline with no credentials.

    python src/scripts/vad_asr_clips.py --episode "Mixed audio session"
    python src/scripts/vad_asr_clips.py --since 2026-08-01 --clips 30
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.timeline.episode_bounds import (
    speech_profile_for_range,
)
from advanced_omi_backend.services.transcript_time import as_utc, transcript_for_range
from advanced_omi_backend.utils.audio_chunk_utils import (
    normalize_wav_peak,
    reconstruct_audio_segment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("vad_asr_clips")

WINDOW = timedelta(minutes=10)
# Enough to tell speech from noise without making the page enormous: 8 s of 16 kHz mono
# is ~256 KB raw, ~341 KB once base64-encoded.
CLIP_SECONDS = 8.0
# A window is a disagreement when VAD is confident and the transcriber produced almost
# nothing. The character floor is per ten-minute window, so it is deliberately low —
# a single "okay" should not count as a transcript.
SPEECH_FLOOR = 0.5
CHARS_FLOOR = 40


@dataclass
class Window:
    started_at: datetime
    ended_at: datetime
    speech: float
    chars: int
    text: str
    conversation_id: str = ""
    offset: float = 0.0
    clip: str = ""
    gain_db: float = 0.0
    kind: str = "disagree"

    @property
    def label(self) -> str:
        return f"{self.started_at:%Y-%m-%d %H:%M}"


async def _locate(when: datetime) -> Optional[tuple[str, float]]:
    """The recording covering an instant, and the offset into it."""

    moment = as_utc(when)
    chunk = (
        await AudioChunkDocument.find(
            {
                "captured_at": {"$lte": moment, "$gte": moment - timedelta(minutes=5)},
                "deleted": {"$ne": True},
            }
        )
        .sort("-captured_at")
        .first_or_none()
    )
    if chunk is None or chunk.captured_at is None:
        return None
    into = (moment - as_utc(chunk.captured_at)).total_seconds()
    if into > (chunk.duration or 10.0) + 1:
        return None
    return chunk.conversation_id, float(chunk.start_time) + into


async def _scan(started_at: datetime, ended_at: datetime) -> list[Window]:
    """Classify every ten-minute window in a range as agree / disagree / quiet."""

    windows: list[Window] = []
    cursor = as_utc(started_at)
    end = as_utc(ended_at)
    while cursor < end:
        nxt = min(cursor + WINDOW, end)
        profile = await speech_profile_for_range(cursor, nxt)
        if not profile.measured_buckets:
            cursor = nxt
            continue
        result = await transcript_for_range(cursor, nxt)
        text = " ".join(item.text for item in result.segments).strip()
        speech = profile.speech_fraction
        if speech >= SPEECH_FLOOR and len(text) < CHARS_FLOOR:
            kind = "disagree"
        elif speech >= SPEECH_FLOOR and len(text) >= CHARS_FLOOR:
            kind = "agree"
        else:
            kind = "quiet"
        windows.append(Window(cursor, nxt, speech, len(text), text[:400], kind=kind))
        cursor = nxt
    return windows


async def _attach_clip(window: Window) -> bool:
    """Extract the clip from the middle of the window. False when audio is gone."""

    middle = window.started_at + (window.ended_at - window.started_at) / 2
    located = await _locate(middle)
    if located is None:
        return False
    conversation_id, offset = located
    try:
        wav = await reconstruct_audio_segment(
            conversation_id, offset, offset + CLIP_SECONDS
        )
    except Exception as error:  # noqa: BLE001 - a missing clip must not stop the page
        log.warning("clip at %s failed: %s", window.label, error)
        return False
    if not wav:
        return False
    wav, window.gain_db = normalize_wav_peak(wav)
    window.conversation_id = conversation_id
    window.offset = round(offset, 1)
    window.clip = base64.b64encode(wav).decode("ascii")
    return True


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>VAD vs ASR — listen and label</title>
<style>
 :root{--bg:#14100e;--card:#191412;--fg:#efe7e1;--dim:#9b8d84;--line:#332a26;
       --accent:#c98a5b;--warn:#e0574a;--ok:#63a375}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
 header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
        padding:9px 16px;z-index:5;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .sp{flex:1}
 kbd{background:#241d1a;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
     font:12px ui-monospace,monospace;color:var(--dim)}
 #list{padding:12px 16px 40vh}
 .w{border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin:0 0 9px;
    background:var(--card);scroll-margin-top:70px}
 .w.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
 .tag{display:inline-block;border:1px solid var(--line);border-radius:999px;
      padding:0 8px;margin-right:6px;font-size:11.5px}
 .disagree{color:var(--warn);border-color:var(--warn)}
 .agree{color:var(--ok);border-color:var(--ok)}
 .meta{color:var(--dim);font-size:12.5px}
 audio{width:100%;margin-top:7px;height:34px}
 pre{white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;color:#cbbfb6;
     margin:6px 0 0;max-height:90px;overflow:auto}
 .lab{font-weight:600;color:var(--accent)}
</style>
<header>
  <b>VAD vs ASR — listen and label</b><span id="pos" class="meta"></span>
  <span class="sp"></span>
  <span class="meta"><kbd>j</kbd><kbd>k</kbd> move · <kbd>space</kbd> play ·
   <kbd>1</kbd> person <kbd>2</kbd> media <kbd>3</kbd> noise <kbd>4</kbd> silence
   <kbd>5</kbd> unclear · <kbd>e</kbd> export</span>
</header>
<div id="list"></div>
<script>
const DATA = __DATA__;
const KEY = "chronicle-vad-asr-labels";
let labels = JSON.parse(localStorage.getItem(KEY) || "{}");
let cur = 0;
const LABELS = {1:"person", 2:"media", 3:"noise", 4:"silence", 5:"unclear"};

function render(){
  const list = document.getElementById("list");
  list.innerHTML = "";
  DATA.forEach((w, i) => {
    const el = document.createElement("div");
    el.className = "w" + (i === cur ? " on" : "");
    el.id = "w" + i;
    const lab = labels[w.id];
    el.innerHTML = `
      <div>
        <span class="tag ${w.kind}">${w.kind}</span>
        <span class="tag">${Math.round(w.speech*100)}% VAD speech</span>
        <span class="tag">${w.chars} ASR chars</span>
        ${w.gain_db ? `<span class="tag">boosted +${w.gain_db} dB</span>` : ""}
        <span class="meta">${w.label} &middot; ${w.conversation_id.slice(0,8)} @ ${w.offset}s</span>
        ${lab ? `<span class="lab">&nbsp;${lab}</span>` : ""}
      </div>
      <audio controls preload="none" src="data:audio/wav;base64,${w.clip}"></audio>
      ${w.text ? `<pre>${w.text}</pre>` : '<pre class="meta">(no transcript in this window)</pre>'}`;
    list.appendChild(el);
  });
  document.getElementById("pos").textContent =
    `${cur+1}/${DATA.length} · ${Object.keys(labels).length} labelled`;
  document.getElementById("w"+cur)?.scrollIntoView({block:"center", behavior:"smooth"});
}

function play(){
  document.querySelectorAll("audio").forEach(a => a.pause());
  const a = document.getElementById("w"+cur)?.querySelector("audio");
  if (a){ a.currentTime = 0; a.play(); }
}

document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  if (LABELS[k]){
    labels[DATA[cur].id] = LABELS[k];
    localStorage.setItem(KEY, JSON.stringify(labels));
    if (cur < DATA.length-1) cur++;
    render(); play(); return;
  }
  if (k === "j" && cur < DATA.length-1){ cur++; render(); play(); }
  else if (k === "k" && cur > 0){ cur--; render(); play(); }
  else if (k === " "){ e.preventDefault(); play(); }
  else if (k === "e"){
    const out = DATA.map(w => ({id:w.id, at:w.label, kind:w.kind, speech:w.speech,
      chars:w.chars, conversation_id:w.conversation_id, offset:w.offset,
      label: labels[w.id] || null}));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],
                                          {type:"application/json"}));
    a.download = "vad-asr-labels.json"; a.click();
  }
});
render();
</script>
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", default="", help="episode title prefix to scan")
    parser.add_argument(
        "--since", default="", help="YYYY-MM-DD, scan from here instead"
    )
    parser.add_argument("--clips", type=int, default=18, help="disagreement clips")
    parser.add_argument("--controls", type=int, default=6, help="agreeing clips")
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument(
        "--out", type=Path, default=Path("/app/data/vad-asr-clips.html")
    )
    args = parser.parse_args()

    zone = ZoneInfo(args.timezone)
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    database = client.chronicle
    await init_beanie(
        database=database, document_models=[Conversation, AudioChunkDocument, User]
    )

    if args.episode:
        episode = await database["timeline_episodes"].find_one(
            {"title": {"$regex": f"^{args.episode}"}}
        )
        if not episode:
            raise SystemExit(f"no episode titled like {args.episode!r}")
        log.info("scanning %s", episode["title"])
        windows = await _scan(episode["started_at"], episode["ended_at"])
    elif args.since:
        start = datetime.fromisoformat(args.since).replace(tzinfo=zone)
        windows = await _scan(start, datetime.now(zone))
    else:
        raise SystemExit("pass --episode or --since")

    counts: dict[str, int] = {}
    for window in windows:
        counts[window.kind] = counts.get(window.kind, 0) + 1
    log.info("windows: %s", counts)

    # Spread the sample across the range rather than taking the first N, so a single bad
    # stretch cannot stand in for the whole thing.
    def spread(items: list[Window], want: int) -> list[Window]:
        if len(items) <= want:
            return items
        step = len(items) / want
        return [items[int(i * step)] for i in range(want)]

    chosen = spread([w for w in windows if w.kind == "disagree"], args.clips)
    chosen += spread([w for w in windows if w.kind == "agree"], args.controls)
    chosen.sort(key=lambda w: w.started_at)

    payload: list[dict[str, Any]] = []
    for index, window in enumerate(chosen, 1):
        if not await _attach_clip(window):
            continue
        payload.append(
            {
                "id": f"{window.started_at.isoformat()}",
                "label": window.started_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                "kind": window.kind,
                "speech": round(window.speech, 3),
                "chars": window.chars,
                "text": escape(window.text),
                "conversation_id": window.conversation_id,
                "offset": window.offset,
                "gain_db": window.gain_db,
                "clip": window.clip,
            }
        )
        log.info("  clip %d/%d %s (%s)", index, len(chosen), window.label, window.kind)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(PAGE.replace("__DATA__", json.dumps(payload)), encoding="utf-8")
    log.info(
        "wrote %s (%.1f MB, %d clips)",
        args.out,
        args.out.stat().st_size / 1e6,
        len(payload),
    )


if __name__ == "__main__":
    asyncio.run(main())
