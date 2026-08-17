#!/usr/bin/env python3
"""Generate a self-contained HTML reviewer for episode boundary decisions.

The gate in ``services/timeline/episode_bounds`` proposes where a long episode may
break, but "a five-minute silence at 04:12" is not something anyone can judge from a
log line. This renders each candidate as a speech strip with its proposed cuts marked,
and — the part that makes it a review rather than a picture — resolves every cut back
to a recording and an offset inside it, so the moment can be played.

Reads only. Decisions live in the browser and are exported as JSON; nothing here
writes to Mongo.

    python src/scripts/episode_bounds_review.py --min-minutes 60
    python src/scripts/episode_bounds_review.py --all --out /app/data/review.html
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.audio_claims import locate_conversation_audio_at
from advanced_omi_backend.services.timeline.episode_bounds import (
    EPISODE_MIN_QUIET,
    BucketState,
    EpisodeBoundsAssessment,
    QuietRun,
    SpeechProfile,
    assess_episode_bounds,
)
from advanced_omi_backend.services.transcript_time import as_utc, transcript_for_range
from advanced_omi_backend.utils.audio_chunk_utils import (
    normalize_wav_peak,
    reconstruct_audio_segment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bounds_review")

# One character per bucket keeps a 16-hour episode's strip under 6 KB of HTML while
# staying legible in the page source.
_GLYPH = {
    BucketState.SPEECH: "S",
    BucketState.SILENT: ".",
    BucketState.NO_CAPTURE: "_",
    BucketState.UNSCORED: "?",
}


def _local(value: datetime, zone: ZoneInfo) -> str:
    return as_utc(value).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


async def _locate(when: datetime) -> Optional[dict[str, Any]]:
    """Which recording covers this instant, and how far into it.

    Uses the chunk carrying the moment, so the offset stays correct across a silence
    trim that renumbered everything around it.
    """

    location = await locate_conversation_audio_at(when)
    if location is None:
        return None
    return {
        "conversation_id": location.conversation_id,
        "offset": round(location.offset_seconds, 1),
    }


COLUMNS = 1000
# A cut is placed in the *middle* of the longest silence, so a clip centred on it is
# nothing but noise floor amplified to the gain cap — every one measured at exactly
# +46 dB. What decides whether a seam is a real boundary is the transition: what was
# happening as the audio went quiet, and what was happening when it came back.
CUT_CLIP_SECONDS = 8.0


def _columns(profile: SpeechProfile) -> list[list[int]]:
    """Downsample the profile to fixed-width columns without losing short silences.

    One pixel can cover many buckets, so point-sampling is not an option: a
    five-minute silence inside a sixteen-hour episode is 30 of 5,760 buckets and lands
    on well under one pixel, so sampling drops the one thing the reviewer is looking
    for. Each column therefore carries both the *mean* speech share (texture, so the
    strip stops being a solid block) and the *minimum* (so any quiet inside the column
    survives), plus flags for gap and unscored.
    """

    if not profile:
        return []
    width = min(COLUMNS, len(profile))
    columns: list[list[int]] = []
    for index in range(width):
        first = int(index * len(profile) / width)
        last = max(first + 1, int((index + 1) * len(profile) / width))
        window = profile.buckets[first:last]
        measured = [b.speech_share for b in window if b.is_measured]
        columns.append(
            [
                round(100 * sum(measured) / len(measured)) if measured else -1,
                round(100 * min(measured)) if measured else -1,
                int(any(b.state is BucketState.NO_CAPTURE for b in window)),
                int(any(b.state is BucketState.UNSCORED for b in window)),
            ]
        )
    return columns


def _quiet_lanes(profile: SpeechProfile, min_quiet: timedelta) -> list[dict[str, Any]]:
    """Every quiet run, flagged for whether it is long enough to carry a boundary.

    Drawn as its own lane so "a pause" and "a candidate seam" stop looking identical,
    which is the single thing the strip has to communicate.
    """

    if not profile:
        return []
    total = len(profile)
    lanes = []
    for run in profile.quiet_runs():
        seconds = run.seconds(profile.bucket_seconds)
        gap = any(
            profile.buckets[i].state is BucketState.NO_CAPTURE
            for i in range(run.first, run.last)
        )
        lanes.append(
            {
                "from": run.first / total,
                "to": run.last / total,
                "minutes": round(seconds / 60, 1),
                "qualifies": seconds >= min_quiet.total_seconds(),
                "gap": gap,
            }
        )
    return lanes


# A bucket counts as "speaking" for clip placement only above this share. A bucket is
# ten seconds and is marked SPEECH on any frame over threshold, so the bucket adjacent
# to a gap is usually 95% silence — clipping there produced noise floor at the +46 dB
# gain cap on nearly every seam. The clip has to be anchored where someone is actually
# talking, or it cannot answer what the boundary separates.
VOICED_SHARE = 0.3


def _run_around(profile: SpeechProfile, bucket: int) -> Optional[QuietRun]:
    """The quiet run a proposed cut sits inside."""

    for run in profile.quiet_runs():
        if run.first <= bucket <= run.last:
            return run
    return None


def _voiced_before(profile: SpeechProfile, bucket: int) -> Optional[int]:
    for index in range(min(bucket, len(profile)) - 1, -1, -1):
        if profile.buckets[index].speech_share >= VOICED_SHARE:
            return index
    return None


def _voiced_after(profile: SpeechProfile, bucket: int) -> Optional[int]:
    for index in range(max(0, bucket), len(profile)):
        if profile.buckets[index].speech_share >= VOICED_SHARE:
            return index
    return None


async def _edge_clip(when: datetime, seconds: float) -> dict[str, Any]:
    """A clip ending at ``when`` if seconds is negative, else starting at it."""

    located = await _locate(when)
    if located is None:
        return {}
    offset = located["offset"]
    start = offset - abs(seconds) if seconds < 0 else offset
    start = max(0.0, start)
    try:
        wav = await reconstruct_audio_segment(
            located["conversation_id"], start, start + abs(seconds)
        )
    except Exception as error:  # noqa: BLE001 - a missing clip is not fatal
        log.warning("edge clip at %s failed: %s", when, error)
        return {}
    if not wav:
        return {}
    wav, gain = normalize_wav_peak(wav)
    return {
        "clip": base64.b64encode(wav).decode("ascii"),
        "gain_db": gain,
        "conversation_id": located["conversation_id"],
    }


async def _episode_payload(
    episode: dict[str, Any], assessment: EpisodeBoundsAssessment, zone: ZoneInfo
) -> dict[str, Any]:
    profile = assessment.profile
    cuts = []
    for cut in assessment.cuts:
        bucket = int(
            (cut - assessment.started_at).total_seconds() / profile.bucket_seconds
        )
        run = _run_around(profile, bucket)
        # Embedded rather than streamed: `stream_audio` answers a Range request with
        # 200 and no Content-Range, so it cannot seek — reaching a cut at minute 132
        # would mean downloading every byte before it.
        before = after = {}
        gap_minutes = 0.0
        if run is not None:
            gap_minutes = round(run.seconds(profile.bucket_seconds) / 60, 1)
            voiced_in = _voiced_before(profile, run.first)
            voiced_out = _voiced_after(profile, run.last)
            if voiced_in is not None:
                edge_in = assessment.started_at + timedelta(
                    seconds=(voiced_in + 1) * profile.bucket_seconds
                )
                before = await _edge_clip(edge_in, -CUT_CLIP_SECONDS)
            if voiced_out is not None:
                edge_out = assessment.started_at + timedelta(
                    seconds=voiced_out * profile.bucket_seconds
                )
                after = await _edge_clip(edge_out, CUT_CLIP_SECONDS)
        cuts.append(
            {
                "at": _local(cut, zone),
                "bucket": bucket,
                "gap_minutes": gap_minutes,
                "before": before,
                "after": after,
            }
        )
    recordings = sorted(
        {
            item
            for entry in episode.get("audio_ranges") or []
            for item in (entry.get("conversation_ids") or [])
        }
    )
    transcript = await transcript_for_range(
        assessment.started_at,
        assessment.ended_at,
        conversation_ids=recordings or None,
    )
    return {
        "episode_id": str(episode.get("episode_id") or ""),
        # Rendered via innerHTML in the page, so a title containing markup must not
        # be able to restructure it.
        "title": escape(str(episode.get("title") or "(untitled)")),
        "kind": episode.get("kind") or "",
        "conversational": bool(episode.get("conversational")),
        "started": _local(assessment.started_at, zone),
        "ended": _local(assessment.ended_at, zone),
        "minutes": round(assessment.duration_seconds / 60, 1),
        "verdict": str(assessment.verdict),
        "vad_suspect": assessment.vad_suspect,
        "measured_pct": round(100 * profile.measured_fraction),
        "speech_pct": round(100 * profile.speech_fraction),
        "longest_quiet_min": round(profile.longest_quiet_seconds / 60, 1),
        "bucket_seconds": profile.bucket_seconds,
        "columns": _columns(profile),
        "quiet": _quiet_lanes(profile, EPISODE_MIN_QUIET),
        "min_quiet_min": EPISODE_MIN_QUIET.total_seconds() / 60,
        "cuts": cuts,
        "recordings": recordings,
        "transcript": transcript.render(str(zone))[:4000],
    }


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>Episode boundary review</title>
<style>
 :root{--bg:#14100e;--card:#191412;--fg:#efe7e1;--dim:#9b8d84;--line:#332a26;
       --speech:#c98a5b;--speech-dim:#6d4a31;--seam:#3fbfa8;--pause:#2c4a44;
       --gap:#4a4340;--unscored:#7a5c8a;--cut:#e0574a;--ok:#63a375;--no:#c1554a}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
 header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
        padding:8px 16px;z-index:5}
 .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .row+.row{margin-top:5px}
 header b{font-size:15px} .sp{flex:1}
 kbd{background:#241d1a;border:1px solid var(--line);border-radius:4px;
     padding:1px 5px;font:12px ui-monospace,monospace;color:var(--dim)}
 .chip{border:1px solid var(--line);border-radius:999px;padding:1px 10px;cursor:pointer;
       font-size:12.5px;color:var(--dim);background:none}
 .chip.on{color:var(--fg);border-color:var(--speech);background:#241a14}
 .key{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--dim)}
 .sw{width:11px;height:11px;border-radius:2px;display:inline-block}
 #list{padding:12px 16px 40vh}
 .ep{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 10px;
     background:var(--card);scroll-margin-top:96px}
 .ep.on{border-color:var(--speech);box-shadow:0 0 0 1px var(--speech)}
 .ep h2{margin:0 0 3px;font-size:15px;font-weight:600}
 .meta{color:var(--dim);font-size:12.5px}
 .tag{display:inline-block;border:1px solid var(--line);border-radius:999px;
      padding:0 8px;margin-right:6px;font-size:11.5px}
 .warn{color:var(--cut);border-color:var(--cut)}
 .v-split{color:var(--seam);border-color:var(--seam)}
 canvas{width:100%;height:110px;display:block;border-radius:4px;background:#0f0c0b;
        margin-top:8px}
 .cuts{margin-top:7px}
 .cutrow{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
 .side{display:flex;flex-direction:column;gap:2px;min-width:260px}
 .side audio{width:260px;height:32px;margin:0}
 .cut{border:1px solid var(--cut);border-radius:6px;padding:3px 8px;font-size:12.5px}
 .cut button{margin-left:6px;background:var(--cut);color:#fff;border:0;
             border-radius:4px;padding:2px 7px;cursor:pointer}
 .d-accept{color:var(--ok);font-weight:600}
 .d-reject{color:var(--no);font-weight:600}
 .d-unsure{color:var(--dim);font-weight:600}
 details{margin-top:7px} summary{cursor:pointer;color:var(--dim);font-size:12.5px}
 pre{white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;color:#cbbfb6;
     max-height:320px;overflow:auto;margin:6px 0 0}
 audio{width:100%;margin-top:8px}
</style>
<header>
  <div class="row">
    <b>Episode boundary review</b>
    <span id="pos" class="meta"></span>
    <span class="sp"></span>
    <span class="meta"><kbd>j</kbd><kbd>k</kbd> move · <kbd>a</kbd> accept ·
      <kbd>r</kbd> reject · <kbd>u</kbd> unsure · <kbd>space</kbd> play cut ·
      <kbd>t</kbd> transcript · <kbd>e</kbd> export</span>
  </div>
  <div class="row">
    <span id="filters"></span>
    <span class="sp"></span>
    <span class="key"><i class="sw" style="background:var(--speech)"></i>speech level</span>
    <span class="key"><i class="sw" style="background:var(--seam)"></i>seam &ge;<span id="mq"></span>min</span>
    <span class="key"><i class="sw" style="background:var(--pause)"></i>shorter pause</span>
    <span class="key"><i class="sw" style="background:var(--gap)"></i>no capture</span>
    <span class="key"><i class="sw" style="background:var(--unscored)"></i>unscored</span>
    <span class="key"><i class="sw" style="background:var(--cut)"></i>proposed cut</span>
  </div>
</header>
<div id="list"></div>
<script>
const DATA = __DATA__;
const CONF = __CONF__;
const KEY = "chronicle-bounds-review";
let decisions = JSON.parse(localStorage.getItem(KEY) || "{}");
let backend = localStorage.getItem("chronicle-backend") || CONF.backend;
let token = localStorage.getItem("chronicle-token") || CONF.token;
let cur = 0, filter = "review";

const FILTERS = [
  ["review", "needs review", ep => ep.verdict !== "within_target"],
  ["split",  "split",        ep => ep.verdict === "split"],
  ["noseam", "no seam",      ep => ep.verdict === "no_seam"],
  ["susp",   "VAD suspect",  ep => ep.vad_suspect],
  ["all",    "all",          () => true],
];
const shown = () => DATA.filter(FILTERS.find(f => f[0] === filter)[2]);

function side(label, s){
  if (!s || !s.clip) return `<span class="meta">${label}: no audio (capture gap)</span>`;
  return `<span class="meta">${label}${s.gain_db?` +${s.gain_db}dB`:""}</span>` +
         `<audio controls preload="none" src="data:audio/wav;base64,${s.clip}"></audio>`;
}

function css(name){ return getComputedStyle(document.documentElement).getPropertyValue(name); }

// Tick spacing that yields 5-10 labels whatever the duration; a 75-minute episode and
// a 956-minute one must not look the same width with no scale.
function tickMinutes(total){
  for (const m of [5,10,15,30,60,120,180,360,720]) if (total / m <= 10) return m;
  return 1440;
}

function draw(cv, ep){
  const W = cv.width = cv.clientWidth * devicePixelRatio;
  const H = cv.height = 110 * devicePixelRatio;
  const g = cv.getContext("2d");
  const AX = 16 * devicePixelRatio;         // time axis band
  const LANE = 14 * devicePixelRatio;       // quiet lane
  const top = AX, bot = H - LANE, plot = bot - top;
  g.clearRect(0,0,W,H);

  const cols = ep.columns, n = cols.length;
  // Capture gaps and unscored first, as full-height context bands.
  for (let i = 0; i < n; i++){
    const x = i / n * W, w = Math.ceil(W / n);
    if (cols[i][2]) { g.fillStyle = css("--gap"); g.fillRect(x, top, w, plot); }
    if (cols[i][3]) { g.fillStyle = css("--unscored"); g.fillRect(x, top, w, plot); }
  }
  // Speech level: mean as the solid area, min as a darker underlay so a column that
  // contains any quiet still reads as lower than one that does not.
  for (let i = 0; i < n; i++){
    const [mean, min] = cols[i];
    if (mean < 0) continue;
    const x = i / n * W, w = Math.ceil(W / n);
    g.fillStyle = css("--speech-dim");
    g.fillRect(x, bot - plot * mean / 100, w, plot * mean / 100);
    g.fillStyle = css("--speech");
    g.fillRect(x, bot - plot * min / 100, w, plot * min / 100);
  }
  // Quiet lane: the actual answer to "where could this break".
  g.fillStyle = "#0f0c0b"; g.fillRect(0, bot, W, LANE);
  for (const q of ep.quiet){
    const x = q.from * W, w = Math.max(devicePixelRatio, (q.to - q.from) * W);
    g.fillStyle = q.qualifies ? css("--seam") : css("--pause");
    g.fillRect(x, bot + 2 * devicePixelRatio, w, LANE - 3 * devicePixelRatio);
  }
  // Time axis.
  const step = tickMinutes(ep.minutes);
  g.fillStyle = css("--dim");
  g.font = `${11 * devicePixelRatio}px ui-monospace,monospace`;
  for (let m = 0; m <= ep.minutes; m += step){
    const x = m / ep.minutes * W;
    g.globalAlpha = 0.25; g.fillRect(x, top, devicePixelRatio, plot); g.globalAlpha = 1;
    if (m) g.fillText(m >= 120 ? (m/60).toFixed(0) + "h" : m + "m",
                      x + 3 * devicePixelRatio, 12 * devicePixelRatio);
  }
  // Proposed cuts on top of everything.
  for (const c of ep.cuts){
    const x = c.bucket / (ep.minutes * 60 / ep.bucket_seconds) * W;
    g.fillStyle = css("--cut");
    g.fillRect(x - devicePixelRatio, 0, 3 * devicePixelRatio, H);
  }
}

function audioUrl(id, offset){
  if (!token || !backend) return null;
  return `${backend.replace(/\/$/, "")}/api/stream_audio/${id}` +
         `?token=${encodeURIComponent(token)}#t=${offset|0}`;
}

function render(){
  const list = document.getElementById("list"), rows = shown();
  if (cur >= rows.length) cur = Math.max(0, rows.length - 1);
  document.getElementById("mq").textContent = (DATA[0]?.min_quiet_min ?? 5);
  document.getElementById("filters").innerHTML = FILTERS.map((f, i) =>
    `<button class="chip ${f[0]===filter?"on":""}" data-f="${f[0]}">${i+1}. ${f[1]}
     (${DATA.filter(f[2]).length})</button>`).join(" ");
  list.innerHTML = "";
  rows.forEach((ep, i) => {
    const d = decisions[ep.episode_id] || "";
    const el = document.createElement("div");
    el.className = "ep" + (i === cur ? " on" : "");
    el.id = "ep" + i;
    const hrs = ep.minutes >= 120 ? ` (${(ep.minutes/60).toFixed(1)}h)` : "";
    el.innerHTML = `
      <h2>${ep.title}</h2>
      <div class="meta">
        <span class="tag ${ep.vad_suspect?"warn":(ep.verdict==="split"?"v-split":"")}">${ep.verdict}</span>
        <span class="tag">${ep.minutes} min${hrs}</span>
        <span class="tag ${ep.measured_pct < 60 ? "warn" : ""}">${ep.measured_pct}% recorded</span>
        <span class="tag">${ep.speech_pct}% speech <i class="meta">of that</i></span>
        <span class="tag">longest quiet ${ep.longest_quiet_min}m</span>
        ${ep.conversational?'<span class="tag">conversational</span>':""}
        ${ep.vad_suspect?'<span class="tag warn">VAD suspect</span>':""}
        ${d?`<span class="d-${d}">${d}</span>`:""}
        <br>${ep.started} &rarr; ${ep.ended}
      </div>
      <canvas></canvas>
      <div class="cuts">${ep.cuts.map((c,j)=>`
        <div class="cutrow">
          <span class="cut">cut ${j+1} @ ${c.at.slice(11)} &middot; ${c.gap_minutes}m quiet</span>
          <span class="side">${side("into the gap", c.before)}</span>
          <span class="side">${side("out of the gap", c.after)}</span>
        </div>`).join("") || '<span class="meta">no cut proposed</span>'}</div>
      <details><summary>transcript (${ep.recordings.length} recording(s))</summary>
        <pre>${ep.transcript || "(none in range)"}</pre></details>`;
    list.appendChild(el);
    draw(el.querySelector("canvas"), ep);
  });
  document.getElementById("pos").textContent =
    `${rows.length ? cur+1 : 0}/${rows.length} · ${Object.keys(decisions).length} decided`;
  document.getElementById("ep"+cur)?.scrollIntoView({block:"center", behavior:"smooth"});
}

function playCut(i, j){
  const card = document.querySelectorAll(".ep")[i];
  const a = card?.querySelectorAll("audio")[j || 0];
  if (!a) return;
  document.querySelectorAll("audio").forEach(x => { if (x !== a) x.pause(); });
  a.currentTime = 0; a.play();
}

function decide(what){
  const ep = shown()[cur]; if (!ep) return;
  decisions[ep.episode_id] = what;
  localStorage.setItem(KEY, JSON.stringify(decisions));
  if (cur < shown().length - 1) cur++;
  render();
}

document.addEventListener("click", e => {
  const f = e.target.closest("button[data-f]");
  if (f){ filter = f.dataset.f; cur = 0; render(); }
});

document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase(), rows = shown();
  if (k >= "1" && k <= "5"){ filter = FILTERS[+k-1][0]; cur = 0; render(); return; }
  if (k === "j" && cur < rows.length-1){ cur++; render(); }
  else if (k === "k" && cur > 0){ cur--; render(); }
  else if (k === "a") decide("accept");
  else if (k === "r") decide("reject");
  else if (k === "u") decide("unsure");
  else if (k === "t"){ const dt = document.getElementById("ep"+cur)?.querySelector("details");
                       if (dt) dt.open = !dt.open; }
  else if (k === " "){ e.preventDefault(); playCut(cur, 0); }
  else if (k === "e"){
    const out = DATA.map(ep => ({episode_id: ep.episode_id, title: ep.title,
      verdict: ep.verdict, minutes: ep.minutes, speech_pct: ep.speech_pct,
      longest_quiet_min: ep.longest_quiet_min, vad_suspect: ep.vad_suspect,
      cuts: ep.cuts.map(c => c.at), decision: decisions[ep.episode_id] || null}));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)],
                                          {type:"application/json"}));
    a.download = "episode-bounds-decisions.json"; a.click();
  }
});

addEventListener("resize", render);
render();
</script>
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-minutes", type=float, default=60.0)
    parser.add_argument("--all", action="store_true", help="every episode")
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument(
        "--out", type=Path, default=Path("/app/data/bounds-review.html")
    )
    parser.add_argument(
        "--backend",
        default="",
        help="backend origin baked into the page, e.g. https://host.example.ts.net",
    )
    parser.add_argument(
        "--token",
        default="",
        help="API key baked in so playback needs no prompt. The page is then a "
        "credential — keep it off shared machines and revoke the key when done.",
    )
    args = parser.parse_args()

    zone = ZoneInfo(args.timezone)
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://mongo:27017"))
    database = client[os.getenv("MONGODB_DATABASE", "chronicle")]
    await init_beanie(
        database=database, document_models=[Conversation, AudioChunkDocument, User]
    )

    episodes = await database["timeline_episodes"].find({}).to_list(length=None)
    floor = 0.0 if args.all else args.min_minutes * 60
    selected = [
        episode
        for episode in episodes
        if (episode["ended_at"] - episode["started_at"]).total_seconds() >= floor
    ]
    selected.sort(key=lambda item: item["started_at"])
    log.info("assessing %d of %d episodes", len(selected), len(episodes))

    payload = []
    for index, episode in enumerate(selected, 1):
        assessment = await assess_episode_bounds(
            episode["started_at"], episode["ended_at"]
        )
        payload.append(await _episode_payload(episode, assessment, zone))
        if index % 10 == 0:
            log.info("  %d/%d", index, len(selected))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        PAGE.replace("__DATA__", json.dumps(payload)).replace(
            "__CONF__", json.dumps({"backend": args.backend, "token": args.token})
        ),
        encoding="utf-8",
    )
    verdicts: dict[str, int] = {}
    for row in payload:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    log.info("wrote %s (%.1f KB)", args.out, args.out.stat().st_size / 1024)
    log.info("verdicts: %s", verdicts)
    log.info("vad_suspect: %d", sum(1 for row in payload if row["vad_suspect"]))


if __name__ == "__main__":
    asyncio.run(main())
