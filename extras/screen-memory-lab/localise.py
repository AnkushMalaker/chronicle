"""Stage 2 -- localise and adjudicate. The half report 08 measured as missing.

Report 08 §6.3 found that a caption index retrieves the *session* readily and the
*outcome* almost never: asked the user's own question, it returned **0%** result
screens in its top 10, and covering all five outcomes needed depth 66. Report 09
§2.1 supplied the architecture from ExtremeWhenBench -- at hour scale, 85% of
failures are *search*, recognition is fine once you are in the right window, and
decomposing into retrieve-then-ground recovers **6.7x** at K=3.

So this script does not try to make retrieval better. It accepts stage 1's top-K
sessions and adds the stage nobody built:

    stage 1 (search)    BM25 over the caption index  -> ranked frames
                        cluster by capture gap       -> candidate SESSIONS
                        take top K=3
    stage 2 (localise)  find candidate CONCLUSION times *inside* each session
                        pull ~12 frames around each at ~30 s spacing
                        -> a VLM that IS allowed to assert
                        -> {outcome, opponent} per boundary
    score               against the AoE4World connector

A session is not a match. The first working version of this script took the last
frame of each candidate session as the boundary and scored **0/5**: stage 1 had
found all the right windows, but the 07-24 21:50-22:34 session contains *two*
matches and ends seven minutes after the later one. Searching within the located
region is not a detail of the architecture, it is the architecture.

Two boundary modes, because the difference between them turned out to be the
interesting number and collapsing them would have hidden it:

    --boundary index      conclusion times come from re-retrieving inside the
                          session with OUTCOME vocabulary, over dense archive
                          frames. Nothing outside the archive is used. Scores 5/5.
    --boundary connector  conclusion times come from the API's started_at +
                          duration. Scores 4/5 -- *worse*, because that is when
                          the game server ended the match, not when the banner
                          was on screen. See report 12 §3b.

**Where the authorship rule lives.** Reports 07 and 08 established that stage-1
captions must NOT carry an authorship rule -- a description is true regardless of
whose screen it is, and demanding an assertion is what manufactured the
attribution traps. Stage 2 is the opposite case: its entire job is to assert an
outcome, so it is exactly where the rule from report 04 belongs. That rule is in
ADJUDICATE_PROMPT below and nowhere else. The two stages want opposite prompts,
which is a large part of why fusing them failed.

    uv run python localise.py --captions out/vlm/captions_e2b.jsonl --label e2b
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from lab.connectors.aoe4world import DEFAULT_GAMERTAG, Match, fetch_matches
from lab.spipe import Archive
from vlm_bench.retrieve_eval import BM25

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "localise"

# A session breaks when nothing was captured for this long. Report 04 measured
# that no cheap signal segments a day well (90-296 stretches for 11 hours), and
# report 09 §2 noted every shipped system takes boundaries from outside the
# pixels. This is deliberately dumb: it is a *capture* gap, not a semantic one,
# and it only has to be good enough to keep two matches from merging.
SESSION_GAP_S = 420

# Frames pulled around a candidate boundary. The window is ASYMMETRIC because a
# result banner appears at the end of a match and persists after it -- there is
# nothing to see 3 minutes early, and everything to see 2 minutes late.
#
# Report 05 §4 measured that result banners live for *tens of seconds*, so the
# sampling rate here has to be finer than that or it steps over the evidence.
# Two runs on the 2026-07-24/25 set made the trade-off visible:
#   +/-180s at n=4  (90s spacing) -> 3/5. Caught m2, stepped over m1 and m5.
#   -60/+180s at n=6 (40s spacing) -> 4/5. Caught m1 and m5, lost m2, whose
#                                    banner sits ~11s BEFORE the window opened.
# So the window has to be wide enough for an imprecise boundary estimate and
# dense enough for a short-lived banner. ~30s spacing across ~6 minutes.
#
# These were tuned on the 2026-07-24/25 matches and then FROZEN before the
# held-out set was run. See report 12.
TAIL_FRAMES = 12
TAIL_BEFORE_S = 120
TAIL_AFTER_S = 240


ADJUDICATE_PROMPT = """\
These screenshots are the LAST frames of one session from a person's own screen \
recording, in chronological order. That person's Age of Empires IV gamertag is \
"{user}".

Decide whether a match involving {user} CONCLUDED in these frames, and if so \
what the result was FOR {user}.

Rules, which matter more than the answer:
- A result screen says "VICTORY" or "DEFEAT" without saying whose it is. It is \
{user}'s result only if these are {user}'s own gameplay frames.
- Another player's profile page, a match-history list, a leaderboard, or a \
spectated game is NOT {user}'s result, even if it shows wins and losses.
- A terminal, editor, browser or chat window discussing a match is NOT a match. \
Text about a game is not a game.
- If you cannot see a conclusive result screen, say so. Do not infer the outcome \
from who looked like they were winning.
import sqlite3

Reply with ONLY a JSON object, no prose and no code fence:
{{"concluded": true|false, "result": "win"|"loss"|null, "opponent": "<gamertag or null>", \
"evidence": "<the on-screen text that decides it>", "confidence": 0.0-1.0}}
"""


@dataclass
class Session:
    frames: list  # list[spipe.Frame]
    score: float

    @property
    def start(self) -> datetime:
        return self.frames[0].timestamp

    @property
    def end(self) -> datetime:
        return self.frames[-1].timestamp

    def __str__(self) -> str:
        return (
            f"{self.start:%m-%d %H:%M}-{self.end:%H:%M} UTC "
            f"({len(self.frames)} frames, score {self.score:.1f})"
        )


@dataclass
class Verdict:
    session: Session
    concluded: bool
    result: str | None
    opponent: str | None
    evidence: str
    confidence: float
    raw: str = ""
    frame_ids: list[int] = field(default_factory=list)
    boundary: datetime | None = None  # the conclusion time actually inspected


# ------------------------------------------------------------------ stage 1


def load_index(
    captions_path: Path | None,
    arch: Archive,
    start: str,
    end: str,
    with_metadata: bool,
    prompt: str = "caption",
) -> tuple[dict[int, str], list]:
    """Build {frame_id: indexed text} plus the frame objects behind it.

    Index content is caption text when supplied, else the frame's stored OCR --
    the report 08 baseline. `with_metadata` appends app name and window title,
    which is report 08 §6.1's mitigation for every captioner misnaming the
    application (E2B said "League of Legends"; codex sol and terra both said
    "strategy game" on frames where the title mattered).
    """
    frames = arch.frames(start, end)
    caps: dict[int, str] = {}
    if captions_path:
        for line in captions_path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            # Report 08 §6.2: the plain `caption` prompt beats `caption_text`
            # as an index in both models tested. Files carry both; take one.
            if not rec.get("ok") or rec.get("prompt") not in (prompt, None):
                continue
            fid = rec.get("frame_id")
            text = rec.get("text") or rec.get("caption") or ""
            if fid is not None and text:
                caps[int(fid)] = text

    docs: dict[int, str] = {}
    for f in frames:
        base = caps.get(f.id, "") if captions_path else f.text
        if not base:
            continue
        if with_metadata:
            base = f"{base} {f.app_name} {f.window_name}".strip()
        docs[f.id] = base
    return docs, frames


def cluster(
    ranked: list[tuple[int, float]],
    by_id: dict,
    top_n_frames: int,
    gap_s: int = SESSION_GAP_S,
) -> list[Session]:
    """Group the highest-ranked frames into temporally contiguous sessions."""
    picked = [(fid, s) for fid, s in ranked[:top_n_frames] if fid in by_id]
    if not picked:
        return []
    picked.sort(key=lambda t: by_id[t[0]].timestamp)

    sessions: list[Session] = []
    cur_frames = [by_id[picked[0][0]]]
    cur_score = picked[0][1]
    for fid, sc in picked[1:]:
        f = by_id[fid]
        if (f.timestamp - cur_frames[-1].timestamp).total_seconds() > gap_s:
            sessions.append(Session(cur_frames, cur_score))
            cur_frames, cur_score = [f], sc
        else:
            cur_frames.append(f)
            cur_score += sc
    sessions.append(Session(cur_frames, cur_score))
    sessions.sort(key=lambda s: s.score, reverse=True)
    return sessions


# ------------------------------------------------------------------ stage 2

# Outcome vocabulary for the within-window search. Report 08 §6.3 measured the
# whole problem: asked "which games did I win and lose" the index returns
# gameplay and 0% result screens; asked "victory defeat" it returns result
# screens at 100%. The user's words and the screen's words are different
# vocabularies, and bridging them is stage 2's first job.
OUTCOME_VOCAB = (
    "victory defeat eliminated surrendered has been eliminated view summary "
    "match summary rematch defeat! victory!"
)


def boundaries_in(
    session: Session, arch: Archive, mode: str, matches: list[Match]
) -> list[datetime]:
    """Candidate conclusion times inside one session.

    This is the localise step proper. A candidate session is 30-45 minutes of
    wall clock and may contain more than one match -- the first run of this
    script scored 0/5 because it assumed "session end" was "match end", and the
    07-24 21:50-22:34 session in fact contains two matches and ends seven
    minutes after the later one. Searching *within* the window is the whole
    point of decomposing search from localisation (report 09 §2.1).
    """
    if mode == "connector":
        # Tier 2 asserts the boundary. Every match end inside the session span,
        # not just the nearest one -- that is what lets one session yield two.
        lo = session.start - timedelta(seconds=SESSION_GAP_S)
        hi = session.end + timedelta(seconds=SESSION_GAP_S)
        return [m.end_dt for m in matches if lo <= m.end_dt <= hi]

    # Index mode: re-retrieve inside the window with OUTCOME vocabulary, over
    # dense archive frames rather than the sparse caption grid. This is AVP's
    # "zoom" -- stage 1 sampled at 1/60 s, stage 2 goes back to the archive at
    # full rate within the located region.
    lo = (session.start - timedelta(seconds=120)).isoformat()
    hi = (session.end + timedelta(seconds=300)).isoformat()
    dense = {f.id: f.text for f in arch.frames(lo, hi) if f.text}
    if not dense:
        return [session.end]
    by_id = {f.id: f for f in arch.frames(lo, hi)}
    ranked = BM25(dense).rank(OUTCOME_VOCAB)

    picks: list[datetime] = []
    for fid, sc in ranked:
        if sc <= 0:
            break
        ts = by_id[fid].timestamp
        if all(abs((ts - p).total_seconds()) > 180 for p in picks):
            picks.append(ts)
        if len(picks) >= 3:
            break
    # Always consider the session's own end too: the zero-OCR banner (f7173)
    # carries no text to retrieve on, so vocabulary search cannot see it.
    if all(abs((session.end - p).total_seconds()) > 180 for p in picks):
        picks.append(session.end)
    return picks


def tail_frames(
    arch: Archive,
    end_dt: datetime,
    n: int = TAIL_FRAMES,
    before_s: int = TAIL_BEFORE_S,
    after_s: int = TAIL_AFTER_S,
) -> list:
    """Frames bracketing a candidate boundary -- where the result banner lives.

    Report 05 §4 measured that result banners live for tens of seconds, so this
    samples a window around the boundary rather than the single nearest frame,
    and weights it after the boundary rather than symmetrically.
    """
    lo = (end_dt - timedelta(seconds=before_s)).isoformat()
    hi = (end_dt + timedelta(seconds=after_s)).isoformat()
    window = [f for f in arch.frames(lo, hi) if f.chunk_path]
    if not window:
        return []
    if len(window) <= n:
        return window
    step = len(window) / n
    return [window[min(int(i * step), len(window) - 1)] for i in range(n)]


def adjudicate(frames: list, user: str, model: str, timeout: int = 300) -> dict:
    """Hand frames to codex and get a structured verdict back."""
    if not frames:
        return {
            "concluded": False,
            "result": None,
            "opponent": None,
            "evidence": "no frames with pixels",
            "confidence": 0.0,
            "raw": "",
        }

    pngs = []
    for f in frames:
        try:
            pngs.append(str(Archive().frame_png(f.id)))
        except Exception as exc:  # chunk missing, ffmpeg failure
            print(f"    ! frame {f.id}: {exc}", file=sys.stderr)
    if not pngs:
        return {
            "concluded": False,
            "result": None,
            "opponent": None,
            "evidence": "no pixels extractable",
            "confidence": 0.0,
            "raw": "",
        }

    cmd = ["codex", "exec", "--skip-git-repo-check", "-m", model, "-i", *pngs]
    proc = subprocess.run(
        cmd,
        input=ADJUDICATE_PROMPT.format(user=user),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # `codex exec` puts the clean answer on stdout; the transcript, banner and
    # token report all go to stderr. Read stdout and do not try to scrape.
    answer = proc.stdout.strip()
    parsed = _parse_json(answer)
    parsed["raw"] = answer
    return parsed


def _parse_json(text: str) -> dict:
    default = {
        "concluded": False,
        "result": None,
        "opponent": None,
        "evidence": "",
        "confidence": 0.0,
    }
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return default
    try:
        got = json.loads(m.group(0))
    except json.JSONDecodeError:
        return default
    default.update({k: got.get(k, v) for k, v in default.items()})
    if got.get("result") in ("win", "loss"):
        default["result"] = got["result"]
    return default


# ------------------------------------------------------------------ scoring


def score(verdicts: list[Verdict], matches: list[Match], tol_s: int = 900) -> dict:
    """Match each verdict to the nearest real match by time, then check outcome.

    A verdict is only credited if it lands within `tol_s` of a real match end --
    otherwise a pipeline that answered "loss" for every session would score well
    on a day with many losses.
    """
    hits, wrong, unmatched = [], [], []
    used: set[int] = set()
    for v in verdicts:
        if not v.concluded or v.result is None:
            continue
        anchor = v.boundary or v.session.end
        best, best_gap = None, None
        for m in matches:
            gap = abs((anchor - m.end_dt).total_seconds())
            if (
                gap <= tol_s
                and m.game_id not in used
                and (best_gap is None or gap < best_gap)
            ):
                best, best_gap = m, gap
        if best is None:
            unmatched.append(v)
            continue
        used.add(best.game_id)
        (hits if v.result == best.result else wrong).append((v, best))
    return {"hits": hits, "wrong": wrong, "unmatched": unmatched, "used": used}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--captions",
        type=Path,
        default=None,
        help="caption jsonl; omit to index stored OCR (the baseline)",
    )
    ap.add_argument("--label", default="run")
    ap.add_argument("--query", default="which Age of Empires games did I win and lose")
    ap.add_argument("--start", required=True, help="UTC ISO")
    ap.add_argument("--end", required=True, help="UTC ISO")
    ap.add_argument(
        "--k", type=int, default=3, help="candidate sessions (report 09: 3)"
    )
    ap.add_argument(
        "--pool", type=int, default=60, help="ranked frames fed to the clusterer"
    )
    ap.add_argument("--boundary", choices=["index", "connector"], default="index")
    ap.add_argument(
        "--metadata",
        action="store_true",
        help="append app name + window title to each index doc",
    )
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--since", default="2026-07-22")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    arch = Archive()
    matches = [
        m
        for m in fetch_matches(args.since)
        if args.start <= m.started_at.replace("Z", "+00:00") <= args.end
    ]

    docs, frames = load_index(args.captions, arch, args.start, args.end, args.metadata)
    by_id = {f.id: f for f in frames}
    print(
        f"index: {len(docs)} docs over {len(frames)} frames "
        f"({'captions' if args.captions else 'stored OCR'}"
        f"{'+metadata' if args.metadata else ''})"
    )
    print(f"truth: {len(matches)} matches in window\n")
    if not docs:
        print("empty index — nothing to do")
        return

    ranked = BM25(docs).rank(args.query)
    sessions = cluster(ranked, by_id, args.pool)[: args.k]
    print(f"stage 1: {len(sessions)} candidate sessions from top-{args.pool} frames")
    for s in sessions:
        print(f"  - {s}")

    verdicts: list[Verdict] = []
    print(f"\nstage 2: adjudicating with {args.model} " f"(boundary={args.boundary})")
    for s in sessions:
        bounds = boundaries_in(s, arch, args.boundary, matches)
        print(f"  session {s} -> {len(bounds)} candidate boundaries")
        for end_dt in bounds:
            tail = tail_frames(arch, end_dt)
            got = adjudicate(tail, DEFAULT_GAMERTAG, args.model)
            v = Verdict(
                session=Session(s.frames, s.score),
                concluded=bool(got["concluded"]),
                result=got["result"],
                opponent=got["opponent"],
                evidence=got["evidence"] or "",
                confidence=float(got["confidence"] or 0),
                raw=got.get("raw", ""),
                frame_ids=[f.id for f in tail],
            )
            v.boundary = end_dt
            verdicts.append(v)
            flag = f"{v.result} vs {v.opponent}" if v.concluded else "no conclusion"
            print(f"    {end_dt:%m-%d %H:%M} -> {flag}  ({v.evidence[:55]})")

    res = score(verdicts, matches)
    n_ok, n_bad = len(res["hits"]), len(res["wrong"])
    print("\n" + "=" * 70)
    print(
        f"outcomes correct at K={args.k}: {n_ok}/{len(matches)} real matches"
        f"   (wrong {n_bad}, unmatched-claim {len(res['unmatched'])})"
    )
    for v, m in res["hits"]:
        print(f"  OK    {m.map_name:<20} truth={m.result:<5} said={v.result}")
    for v, m in res["wrong"]:
        print(f"  WRONG {m.map_name:<20} truth={m.result:<5} said={v.result}")
    for m in matches:
        if m.game_id not in res["used"]:
            print(
                f"  MISS  {m.map_name:<20} truth={m.result:<5} (no session reached it)"
            )

    # The answer the question actually asked for: "I played xyz games, these
    # wins, losses, screenshots." Outcome comes from stage 2 (pixels), opponent
    # and map come from the connector -- report 12 §3c measured that pixels get
    # the outcome right 5/5 and the gamertag wrong 1/5, and report 07 §4's rule
    # is that identity comes from the system of record. Each row carries the
    # frame that proves it, so nothing here is unfalsifiable.
    print("\n" + "=" * 70)
    answered = sorted(res["hits"] + res["wrong"], key=lambda t: t[1].started_at)
    w = sum(1 for v, m in answered if v.result == "win")
    print(
        f"ANSWER: you played {len(answered)} games I can see the end of "
        f"— {w} won, {len(answered)-w} lost"
    )
    for v, m in answered:
        shot = v.frame_ids[len(v.frame_ids) // 2] if v.frame_ids else "?"
        print(
            f"  {m.start_dt:%a %H:%M} UTC  {v.result.upper():<4} on {m.map_name:<18} "
            f"vs {m.opponent:<18} [frame {shot}]"
        )
    # Distinguish "we never recorded it" from "the pipeline did not reach it".
    # Conflating them lets a retrieval failure masquerade as a capture gap, which
    # is the same class of false comfort this project keeps catching.
    missed = [m for m in matches if m.game_id not in res["used"]]
    if missed:
        con = sqlite3.connect(
            f"file:{Path.home()}/.screenpipe/db.sqlite?mode=ro", uri=True
        )
        norec, notreached = [], []
        for m in missed:
            n = con.execute(
                "SELECT COUNT(*) FROM frames WHERE timestamp>=? AND timestamp<=?",
                (m.start_dt.isoformat(), m.end_dt.isoformat()),
            ).fetchone()[0]
            (norec if n == 0 else notreached).append(m)
        if norec:
            print(f"  ({len(norec)} more played but never recorded — capture gap)")
        if notreached:
            print(
                f"  ({len(notreached)} more played AND recorded, but this pipeline "
                f"did not reach: {', '.join(m.map_name for m in notreached)})"
            )

    path = OUT / f"{args.label}_{args.boundary}{'_meta' if args.metadata else ''}.json"
    path.write_text(
        json.dumps(
            {
                "args": vars(args)
                | {
                    "captions": str(args.captions),
                    "start": args.start,
                    "end": args.end,
                },
                "sessions": [str(s) for s in sessions],
                "verdicts": [
                    {
                        "session": str(v.session),
                        "boundary": str(v.boundary),
                        "concluded": v.concluded,
                        "result": v.result,
                        "opponent": v.opponent,
                        "evidence": v.evidence,
                        "confidence": v.confidence,
                        "frame_ids": v.frame_ids,
                    }
                    for v in verdicts
                ],
                "score": {
                    "correct": n_ok,
                    "wrong": n_bad,
                    "matches": len(matches),
                    "unmatched_claims": len(res["unmatched"]),
                },
            },
            indent=1,
            default=str,
        )
    )
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
