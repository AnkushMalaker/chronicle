"""Verify the typographic-salience claim beyond the two AoE4 result screens.

The original claim was a *recall* result on a single category: the two captured
match-result frames rank 7th and 9th of 2821 by salience. That is a weak claim
in three ways. It never measured precision -- what else is at the top. It never
tested a category other than a game. And "of 2821" overstates the field, since
only frames with their own `elements` rows are rankable at all.

This script measures the claim under the condition the pipeline actually uses:
salience picks ~6 hint frames *per 12-minute window*, not 6 per day. So the
question is not "what global rank does a decisive frame get" but "does the
decisive frame make its own window's shortlist".

Metrics produced:

1. Coverage -- what fraction of frames are rankable, by day and by app.
2. Precision@K -- the top K frames globally, dumped for classification.
3. Per-episode recall -- decisive frames are grouped into contiguous episodes,
   and each episode is scored on whether any of its frames reaches its window's
   top-N. Episodes, not frames: a "Defeat" banner held for 40 seconds is one
   event, and surfacing any one of its frames is a success.
4. The same episodes scored by text length, which is what Chronicle ranks by
   today, as a baseline.

Decisive frames are located by category semantics -- window titles, text
markers -- never by consulting salience. Choosing them from the ranking would
make the test circular.

Run:
    uv run python verify_salience.py --top 120 --window-minutes 12 --hints 6
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

from lab.layout import banner_transience, salience_map
from lab.spipe import open_archive

OUT = Path(__file__).resolve().parent / "out" / "verify"
EPISODE_GAP = timedelta(minutes=2)


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --------------------------------------------------------------- categories
#
# Each predicate names decisive frames by its own semantics. None of them looks
# at salience, text height, or whether the frame owns element rows.


def cat_aoe_results(frames):
    """AoE4 post-match result screens: an outcome word plus match-summary chrome."""
    hits = []
    for f in frames:
        t = f.text.lower()
        outcome = "victory" in t or "defeat" in t or "ory" == t.strip()
        if outcome and any(
            k in t for k in ("rated", "elo", "team", "civilization", "score")
        ):
            hits.append(f.id)
    return hits


def cat_video_titles(frames):
    """Frames of a distinct video/episode, identified purely from the title bar."""
    hits = []
    for f in frames:
        m = re.match(r"^(.*?)\s+[—-]\s+Zen Browser$", f.window_name or "")
        if not m:
            continue
        low = m.group(1).strip().lower()
        if (
            "youtube" in low
            or "episode" in low
            or "miruro" in low
            or low.startswith("watch ")
        ):
            hits.append(f.id)
    return hits


def cat_terminal_outcomes(frames):
    """Frames where a command reported an outcome in a terminal."""
    pat = re.compile(
        r"(\b\d+ passed\b|\b\d+ failed\b|\btests? failed\b|\bbuild failed\b|"
        r"\btraceback \(most recent call last\)|\b\d+ files? changed\b|"
        r"\bpermission denied\b|\bfatal:)",
        re.I,
    )
    return [f.id for f in frames if pat.search(f.text)]


def cat_notices(frames):
    """Frames showing a notice or modal that announces a state change."""
    pat = re.compile(
        r"(\bexpires? in\b|\bwill end\b|\bdisconnected\b|\bconnection lost\b|"
        r"\bunsaved changes\b|\bupdate available\b|\bout of credits\b|"
        r"\bquota exceeded\b|\brate limit\b|\bexceeded\b.{0,20}\bcap\b)",
        re.I,
    )
    return [f.id for f in frames if pat.search(f.text)]


CATEGORIES = {
    "aoe4_result_screen": cat_aoe_results,
    "video_watched": cat_video_titles,
    "terminal_outcome": cat_terminal_outcomes,
    "notice_or_modal": cat_notices,
}


def episodes(frame_ids, by_id):
    """Group decisive frames into contiguous runs separated by >EPISODE_GAP."""
    ordered = sorted(frame_ids, key=lambda i: by_id[i].timestamp)
    groups = []
    for fid in ordered:
        f = by_id[fid]
        if groups and f.timestamp - by_id[groups[-1][-1]].timestamp <= EPISODE_GAP:
            groups[-1].append(fid)
        else:
            groups.append([fid])
    return groups


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=120)
    ap.add_argument("--window-minutes", type=int, default=12)
    ap.add_argument("--hints", type=int, default=6)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    arc = open_archive()
    lo, hi = arc.span()
    frames = arc.frames(lo.isoformat(), (hi + timedelta(seconds=1)).isoformat())
    ids = [f.id for f in frames]
    by_id = {f.id: f for f in frames}
    print(f"archive {lo.date()} -> {hi.date()}  frames={len(frames)}")

    sal = {}
    for batch in chunked(ids, 800):
        sal.update(salience_map(arc, batch))
    rankable = {k: v for k, v in sal.items() if v.top_text and v.top_height > 0}

    ocr_frames = [f for f in frames if f.text_source == "ocr"]
    print(
        f"rankable {len(rankable)}/{len(ids)} frames ({100*len(rankable)/len(ids):.1f}%); "
        f"of OCR frames only: {len(rankable)}/{len(ocr_frames)} "
        f"({100*len(rankable)/max(1,len(ocr_frames)):.1f}%)"
    )

    trans = banner_transience(rankable, ids)

    # ------------------------------------------------------- global ranking
    order = sorted(rankable.values(), key=lambda s: -s.score)
    grank = {s.frame_id: i + 1 for i, s in enumerate(order)}

    top = []
    for s in order[: args.top]:
        f = by_id[s.frame_id]
        top.append(
            {
                "rank": grank[s.frame_id],
                "frame_id": s.frame_id,
                "local": f.local_time.strftime("%m-%d %H:%M:%S"),
                "app": f.app_name or "(empty)",
                "window": (f.window_name or "")[:70],
                "top_text": s.top_text,
                "banner": s.banner_texts[:8],
                "height": round(s.top_height, 4),
                "transience": round(trans.get(s.frame_id, -1), 2),
                "text_excerpt": " ".join(f.text.split())[:300],
            }
        )
    (OUT / "top_salient.json").write_text(json.dumps(top, indent=1))
    print(
        f"\ntop-{args.top} app mix: {dict(Counter(r['app'] for r in top).most_common())}"
    )

    # --------------------------------------------- per-window shortlist rank
    # This is the deployment condition: each window offers `hints` frames.
    wsize = timedelta(minutes=args.window_minutes)
    win_of, wmembers = {}, {}
    for f in frames:
        idx = int((f.timestamp - lo) / wsize)
        win_of[f.id] = idx
        wmembers.setdefault(idx, []).append(f.id)

    def shortlist(metric, idx, n):
        """Top-n frame ids in window idx by `metric` (higher = better)."""
        cands = [(metric(i), i) for i in wmembers[idx] if metric(i) is not None]
        cands.sort(key=lambda p: -p[0])
        return [i for _, i in cands[:n]]

    sal_metric = lambda i: rankable[i].score if i in rankable else None
    len_metric = lambda i: len(by_id[i].text) or None

    results = {}
    for name, fn in CATEGORIES.items():
        decisive = fn(frames)
        eps = episodes(decisive, by_id)
        rows = []
        for ep in eps:
            wins = {win_of[i] for i in ep}
            sal_hit = any(i in shortlist(sal_metric, win_of[i], args.hints) for i in ep)
            len_hit = any(i in shortlist(len_metric, win_of[i], args.hints) for i in ep)
            ranked = [i for i in ep if i in rankable]
            first = by_id[ep[0]]
            rows.append(
                {
                    "start_local": first.local_time.strftime("%m-%d %H:%M:%S"),
                    "frames": len(ep),
                    "rankable_frames": len(ranked),
                    "windows": sorted(wins),
                    "salience_in_top_n": sal_hit,
                    "textlen_in_top_n": len_hit,
                    "best_global_rank": min((grank[i] for i in ranked), default=None),
                    "best_top_text": next(
                        (
                            rankable[i].top_text
                            for i in sorted(ranked, key=lambda x: grank[x])
                        ),
                        None,
                    ),
                    "window_title": (first.window_name or "")[:60],
                    "app": first.app_name or "(empty)",
                    "example_frame": ep[0],
                }
            )
        n = len(rows)
        results[name] = {
            "episodes": n,
            "episodes_with_any_rankable_frame": sum(
                1 for r in rows if r["rankable_frames"]
            ),
            "salience_hits": sum(1 for r in rows if r["salience_in_top_n"]),
            "textlen_hits": sum(1 for r in rows if r["textlen_in_top_n"]),
            "rows": rows,
        }

    print(
        f"\n--- per-episode recall, top-{args.hints} hints per "
        f"{args.window_minutes}-minute window ---"
    )
    print(
        f"{'category':<20} {'episodes':>8} {'rankable':>9} {'salience':>9} {'textlen':>8}"
    )
    for name, r in results.items():
        print(
            f"{name:<20} {r['episodes']:>8} "
            f"{r['episodes_with_any_rankable_frame']:>9} "
            f"{r['salience_hits']:>9} {r['textlen_hits']:>8}"
        )

    (OUT / "category_recall.json").write_text(json.dumps(results, indent=1))
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "frames": len(ids),
                "ocr_frames": len(ocr_frames),
                "rankable": len(rankable),
                "window_minutes": args.window_minutes,
                "hints": args.hints,
                "top_app_mix": dict(Counter(r["app"] for r in top).most_common()),
                "recall": {
                    k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                    for k, v in results.items()
                },
            },
            indent=1,
        )
    )

    print("\n--- episodes salience missed (first 12) ---")
    for name, r in results.items():
        miss = [x for x in r["rows"] if not x["salience_in_top_n"]]
        for x in miss[:3]:
            print(
                f"{name:<20} {x['start_local']}  frames={x['frames']:<4} "
                f"rankable={x['rankable_frames']:<4} {x['app'][:12]:<12} "
                f"{(x['window_title'] or x['best_top_text'] or '')[:44]}"
            )
    print(f"\nwrote {OUT}/")


if __name__ == "__main__":
    main()
