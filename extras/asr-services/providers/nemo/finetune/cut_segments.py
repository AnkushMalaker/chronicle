"""Slice source WAVs at the segment boundaries from segment_by_alignment.py and write a
NeMo training manifest of the short, correctly-targeted clips.

Tool-agnostic: consumes segments.jsonl ({audio_file_name, seg_start, seg_end, text,
review}) + the full-clip WAV dir (from make_manifest.py --all). Emits one WAV per segment
and a NeMo manifest line per segment ({audio_filepath, text, duration, target_lang}).

By default skips segments flagged review=true (low GT<->hyp match) — pass --keep-flagged
to include them.

Usage:
    python cut_segments.py --segments segments.jsonl --wav-dir /home/ft/data_full/wav \
        --out-dir /home/ft/data_seg --target-lang hi-IN
"""

import argparse
import json
from pathlib import Path

import soundfile as sf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", required=True)
    ap.add_argument(
        "--wav-dir",
        required=True,
        help="dir of full-clip WAVs (stem = audio_file_name stem)",
    )
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--target-lang", default="hi-IN")
    ap.add_argument("--min-seconds", type=float, default=0.5)
    ap.add_argument("--keep-flagged", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    seg_wav_dir = out / "wav"
    seg_wav_dir.mkdir(parents=True, exist_ok=True)
    wav_dir = Path(args.wav_dir)

    # cache decoded source audio so we read each full clip once
    cache: dict[str, tuple] = {}
    n_seg = n_skip = 0
    with open(out / "train.json", "w") as mf:
        for line in open(args.segments):
            s = json.loads(line)
            if s.get("review") and not args.keep_flagged:
                n_skip += 1
                continue
            dur = s["seg_end"] - s["seg_start"]
            if dur < args.min_seconds or not s["text"].strip():
                n_skip += 1
                continue
            name = s["audio_file_name"]
            stem = Path(name).stem
            if stem not in cache:
                data, sr = sf.read(str(wav_dir / f"{stem}.wav"), dtype="float32")
                if data.ndim > 1:
                    data = data.mean(axis=1)
                cache[stem] = (data, sr)
            data, sr = cache[stem]
            a = max(0, int(s["seg_start"] * sr))
            b = min(len(data), int(s["seg_end"] * sr))
            if b - a < int(args.min_seconds * sr):
                n_skip += 1
                continue
            seg_name = f"{stem}_{int(s['seg_start']*1000):07d}_{int(s['seg_end']*1000):07d}.wav"
            seg_path = seg_wav_dir / seg_name
            sf.write(str(seg_path), data[a:b], sr, subtype="PCM_16")
            mf.write(
                json.dumps(
                    {
                        "audio_filepath": str(seg_path),
                        "text": s["text"],
                        "duration": round((b - a) / sr, 3),
                        "target_lang": args.target_lang,
                        "audio_file_name": seg_name,
                        "source_clip": name,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            n_seg += 1
    print(
        f"wrote {n_seg} segment WAVs + manifest, skipped {n_skip} "
        f"-> {out / 'train.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
