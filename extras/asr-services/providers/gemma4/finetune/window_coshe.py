"""Window CoSHE clips into <=max-seconds segments with EXACT ground-truth targets.

Input is `fa_words.jsonl` — a faithful forced alignment of the CoSHE ground-truth
transcript (per clip: the GT word sequence, each with start/end seconds). This is the
VibeVoice-ASR data recipe: a timestamped source gives word timings, and we map the exact
GT text onto that timeline. Because the alignment IS the GT (word sequence == GT.split()),
windowing is a lossless contiguous partition — concatenating window texts reproduces the
full transcript.

Why window: Gemma4's audio encoder hears <=~30s (E2B hard-caps audio at ~31s / 786 tok),
and CoSHE clips run ~57s median. The prior finetune used a "first-30s + proportional
char-truncation" hack (no timestamps were available then); with real word timings we can
cut honest <=30s (audio, text) pairs and train/eval on the true windowed task.

Cuts fall at WORD boundaries, preferring an inter-word silence (gap >= --min-gap) near the
window tail so boundaries land in pauses rather than mid-utterance.

Out: windows.jsonl, one row per window:
  {audio_file_name, win_idx, n_wins, start, end, dur, n_words, text, cut_gap}
"""

import argparse
import json
import statistics as st
from pathlib import Path


def window_clip(words, max_s, min_s, min_gap):
    """Partition `words` (list of {word,start,end}) into contiguous [i,j] index spans,
    each spanning <= max_s seconds, preferring to end at a silence >= min_gap."""
    spans = []
    i, n = 0, len(words)
    while i < n:
        wstart = words[i]["start"]
        j = i
        while j + 1 < n and words[j + 1]["end"] - wstart <= max_s:
            j += 1
        cut_gap = 0.0
        if j + 1 < n:  # more words remain -> consider cutting at a pause
            best_k, best_gap = j, -1.0
            for k in range(j, i, -1):
                if words[k]["end"] - wstart < min_s:
                    break
                gap = words[k + 1]["start"] - words[k]["end"]
                if gap > best_gap:
                    best_gap, best_k = gap, k
            if best_gap >= min_gap:
                j, cut_gap = best_k, best_gap
        spans.append((i, j, cut_gap))
        i = j + 1
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fa", default="/home/ft/fa_words.jsonl")
    ap.add_argument("--exclude", default="", help="comma-sep audio_file_names to drop")
    ap.add_argument("--out", default="/home/coshe_windowed/windows.jsonl")
    ap.add_argument("--max_seconds", type=float, default=28.0)
    ap.add_argument("--min_seconds", type=float, default=5.0)
    ap.add_argument("--min_gap", type=float, default=0.25)
    ap.add_argument("--pad", type=float, default=0.1)
    args = ap.parse_args()

    drop = {x for x in args.exclude.split(",") if x}
    rows = [json.loads(l) for l in open(args.fa)]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    out = open(args.out, "w")
    n_clips = n_wins = n_dropped = 0
    durs, wins_per_clip, mismatches = [], [], 0
    for r in rows:
        name = r["audio_file_name"]
        if name in drop:
            n_dropped += 1
            continue
        words = r["words"]
        if not words:
            continue
        spans = window_clip(words, args.max_seconds, args.min_seconds, args.min_gap)
        n_clips += 1
        wins_per_clip.append(len(spans))
        # lossless check: contiguous partition reproduces the GT word sequence
        flat = [words[k]["word"] for a, b, _ in spans for k in range(a, b + 1)]
        if flat != [w["word"] for w in words]:
            mismatches += 1
        for wi, (a, b, cut_gap) in enumerate(spans):
            start = max(0.0, words[a]["start"] - args.pad)
            end = words[b]["end"] + args.pad
            text = " ".join(words[k]["word"] for k in range(a, b + 1))
            dur = round(end - start, 3)
            durs.append(dur)
            out.write(
                json.dumps(
                    {
                        "audio_file_name": name,
                        "win_idx": wi,
                        "n_wins": len(spans),
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "dur": dur,
                        "n_words": b - a + 1,
                        "text": text,
                        "cut_gap": round(cut_gap, 3),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            n_wins += 1
    out.close()

    from collections import Counter

    c = Counter(wins_per_clip)
    print(f"clips kept={n_clips} dropped(foreign)={n_dropped}  windows={n_wins}")
    print(f"partition-mismatch clips (should be 0): {mismatches}")
    print(f"windows/clip: {dict(sorted(c.items()))}")
    print(
        f"window dur s: min {min(durs):.1f}  med {st.median(durs):.1f}  "
        f"mean {st.mean(durs):.1f}  max {max(durs):.1f}  p95 {sorted(durs)[int(len(durs)*0.95)]:.1f}"
    )
    over = sum(1 for d in durs if d > 30.0)
    print(f"windows > 30s (should be ~0): {over}")
    print(f"out -> {args.out}")


if __name__ == "__main__":
    main()
