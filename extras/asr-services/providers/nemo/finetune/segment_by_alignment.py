"""Segment long CoSHE clips into <=N-second windows with CORRECT ground-truth targets,
using a timestamped ASR hypothesis as the timing source (forced-alignment substitute).

Why: RNN-T loss is O(T*U*V) -> a 54s clip OOMs even at batch=1 (see
[[nemotron-coshe-finetune]]). The fix is short clips, but CoSHE has no word timestamps,
so we can't cut the transcript to match a shorter audio window. Solution: run a
timestamped ASR (Deepgram Nova-3 multilingual / Google Chirp), use its WORD TIMINGS to
find cut points, and map our exact GT text onto that timeline.

This stage is TOOL-AGNOSTIC: it consumes a normalized per-clip word-timestamp JSONL:
    {"audio_file_name": "...", "words": [{"word": "...", "start": 1.2, "end": 1.4}, ...]}
A thin adapter (see deepgram_words.py / chirp_words.py, TODO) converts each provider's
response into that shape. GT text comes from the CoSHE benchmark jsonl ("transcription").

Pipeline per clip:
  1. romanize GT words and hyp words (IndicXlit) so Devanagari-GT aligns to a hyp in
     either script;
  2. sequence-align GT<->hyp (difflib) and assign each GT word an approx time (matched
     words take the hyp time; unmatched GT words interpolate between matched anchors);
  3. greedily pack GT words into <=--max-seconds segments, preferring to cut at a hyp
     PAUSE (word gap >= --min-gap) so boundaries fall in silence;
  4. emit one segment row per window: {audio_file_name, seg_start, seg_end, text, ...}
     plus a confidence flag (match_ratio) so low-confidence segments can be reviewed.

A later step (cut_segments.py, TODO) slices the WAVs at [seg_start, seg_end] and writes a
NeMo manifest. Nothing here is provider-specific or destructive.

Usage:
    python segment_by_alignment.py --timestamps hyp_words.jsonl \
        --gt nemotron_auto_full.jsonl --out segments.jsonl --max-seconds 18 --min-gap 0.3
"""

import argparse
import json
from difflib import SequenceMatcher
from pathlib import Path

try:
    from mlexp.utils.indicxlit import romanize as _romanize
except Exception:  # script may run where mlexp/IndicXlit isn't importable
    _romanize = None


def romanize_words(words: list[str]) -> list[str]:
    """Lowercase romanized form per word for cross-script alignment. Falls back to a
    plain lowercase when IndicXlit isn't available (Latin-only hyps still align)."""
    if _romanize is None:
        return [w.lower() for w in words]
    # romanize the joined text once (IndicXlit is per-word internally + cached), re-split
    rom = _romanize(" ".join(words)).lower().split()
    # keep length aligned with input; fall back per-word if the join changed token count
    if len(rom) == len(words):
        return rom
    return [_romanize(w).lower() for w in words]


def assign_times(gt_words, hyp_words):
    """Return a list of (start, end) per GT word by aligning to the timed hyp words.

    Matched GT words inherit the hyp word's [start,end]; runs of unmatched GT words are
    linearly interpolated between the surrounding matched anchors. Returns (times,
    match_ratio) where match_ratio is the fraction of GT words that matched a hyp word.
    """
    rom_gt = romanize_words([w["w"] for w in gt_words])
    rom_hyp = romanize_words([w["word"] for w in hyp_words])
    sm = SequenceMatcher(a=rom_gt, b=rom_hyp, autojunk=False)

    times = [None] * len(gt_words)
    matched = 0
    for tag, i1, i2, j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                h = hyp_words[j1 + k]
                times[i1 + k] = (float(h["start"]), float(h["end"]))
                matched += 1

    # interpolate the None runs between anchors
    n = len(times)
    anchors = [(i, t) for i, t in enumerate(times) if t is not None]
    if not anchors:
        return None, 0.0
    # head/tail extrapolation: clamp to first/last anchor time
    first_i, first_t = anchors[0]
    last_i, last_t = anchors[-1]
    for i in range(first_i):
        times[i] = (first_t[0], first_t[0])
    for i in range(last_i + 1, n):
        times[i] = (last_t[1], last_t[1])
    # interior gaps
    for (ia, ta), (ib, tb) in zip(anchors, anchors[1:]):
        gap = ib - ia
        if gap <= 1:
            continue
        t0, t1 = ta[1], tb[0]
        for k in range(1, gap):
            frac = k / gap
            t = t0 + (t1 - t0) * frac
            times[ia + k] = (t, t)
    return times, matched / max(1, len(gt_words))


def pack_segments(gt_words, times, hyp_words, max_seconds, min_gap):
    """Greedily group GT words into <=max_seconds windows, preferring cut points that
    coincide with a hyp pause (a gap >= min_gap between consecutive hyp words)."""
    # precompute pause times (end of a hyp word that is followed by a >=min_gap silence)
    pauses = []
    for a, b in zip(hyp_words, hyp_words[1:]):
        if float(b["start"]) - float(a["end"]) >= min_gap:
            pauses.append((float(a["end"]) + float(b["start"])) / 2.0)

    segs = []
    cur_start_idx = 0
    seg_t0 = times[0][0]
    for i in range(len(gt_words)):
        cur_t1 = times[i][1]
        if cur_t1 - seg_t0 >= max_seconds and i > cur_start_idx:
            # find nearest pause at/before cur_t1 within this window, else cut here
            window_pauses = [p for p in pauses if seg_t0 < p <= cur_t1]
            cut_t = window_pauses[-1] if window_pauses else cur_t1
            segs.append((cur_start_idx, i, seg_t0, cut_t))
            cur_start_idx = i
            seg_t0 = cut_t  # contiguous: next segment starts where this one cut
    # final segment
    segs.append((cur_start_idx, len(gt_words), seg_t0, times[-1][1]))

    # merge a too-short final fragment (< 1/3 max) back into the previous segment
    if len(segs) >= 2 and (segs[-1][3] - segs[-1][2]) < max_seconds / 3:
        s_i, _e_i, t0, _t1 = segs[-2]
        last = segs[-1]
        segs[-2] = (s_i, last[1], t0, last[3])
        segs.pop()
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--timestamps", required=True, help="normalized word-timestamp JSONL"
    )
    ap.add_argument(
        "--gt", required=True, help="CoSHE jsonl with audio_file_name + transcription"
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-seconds", type=float, default=18.0)
    ap.add_argument("--min-gap", type=float, default=0.3)
    ap.add_argument(
        "--min-match-ratio",
        type=float,
        default=0.4,
        help="flag clips whose GT<->hyp match ratio is below this for review",
    )
    args = ap.parse_args()

    gt = {}
    for line in open(args.gt):
        r = json.loads(line)
        if "transcription" in r:
            gt[r["audio_file_name"]] = r["transcription"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_clip = n_seg = n_flag = 0
    with open(out_path, "w") as out_f:
        for line in open(args.timestamps):
            ts = json.loads(line)
            name = ts["audio_file_name"]
            if name not in gt or not ts.get("words"):
                continue
            gt_words = [{"w": w} for w in gt[name].split()]
            hyp_words = ts["words"]
            times, ratio = assign_times(gt_words, hyp_words)
            if times is None:
                continue
            flagged = ratio < args.min_match_ratio
            n_flag += flagged
            for s_i, e_i, t0, t1 in pack_segments(
                gt_words, times, hyp_words, args.max_seconds, args.min_gap
            ):
                text = " ".join(gt_words[k]["w"] for k in range(s_i, e_i))
                out_f.write(
                    json.dumps(
                        {
                            "audio_file_name": name,
                            "seg_start": round(t0, 3),
                            "seg_end": round(t1, 3),
                            "duration": round(t1 - t0, 3),
                            "text": text,
                            "match_ratio": round(ratio, 3),
                            "review": flagged,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                n_seg += 1
            n_clip += 1
    print(
        f"{n_clip} clips -> {n_seg} segments "
        f"({n_seg/max(1,n_clip):.1f}/clip), {n_flag} clips flagged for review "
        f"-> {out_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
