"""Materialize the windowed CoSHE training set: slice each train/val window's audio out
of the CoSHE parquet and write 16k-mono WAVs + a manifest, honoring the clip-level
20/10/70 split (windows inherit their clip's split, so no clip leaks across splits).

Consumes windows.jsonl from window_coshe.py (honest <=30s GT-accurate windows). Test
clips are NOT sliced here — they are kept whole for windowed-stitch evaluation; we only
emit their clip list + per-window text.

Out (under --out_dir):
  audio/<clip>__w<idx>.wav     16k mono int16, one per train/val window
  manifest.jsonl               {name, clip, win_idx, split, audio, dur, text}
  windowed_split.json          {train:[names], val:[names], test_clips:[clips]}
"""

import argparse
import glob
import io
import json
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf


def safe(clip, idx):
    return f"{clip.replace('.wav','')}__w{idx}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="/home/coshe_windowed/windows_v2.jsonl")
    ap.add_argument("--split", default="/home/coshe_windowed/split_20_10_70.json")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--parquet_glob", default="/home/coshe/data/eval-*.parquet")
    ap.add_argument("--out_dir", default="/home/coshe_windowed")
    ap.add_argument(
        "--materialize",
        default="train,val",
        help="which splits get sliced WAVs (test kept whole for stitch eval)",
    )
    args = ap.parse_args()

    drop = {x for x in args.exclude.split(",") if x}
    mat = set(args.materialize.split(","))
    split = json.load(open(args.split))
    clip_split = {}
    for s in ("train", "val", "test"):
        for c in split[s]:
            if c not in drop:
                clip_split[c] = s

    wins_by_clip = defaultdict(list)
    for line in open(args.windows):
        r = json.loads(line)
        if r["audio_file_name"] in drop:
            continue
        wins_by_clip[r["audio_file_name"]].append(r)

    out_dir = Path(args.out_dir)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    man = open(out_dir / "manifest.jsonl", "w")
    split_out = {
        "train": [],
        "val": [],
        "test_clips": sorted(c for c, s in clip_split.items() if s == "test"),
    }

    want_clips = {c for c, s in clip_split.items() if s in mat}
    written = 0
    for f in sorted(glob.glob(args.parquet_glob)):
        t = pq.ParquetFile(f).read(columns=["audio_file_name", "audio"])
        names = t["audio_file_name"].to_pylist()
        for ci, name in enumerate(names):
            if name not in want_clips:
                continue
            s = clip_split[name]
            wav, sr = sf.read(io.BytesIO(t["audio"][ci].as_py()["bytes"]))
            if wav.ndim > 1:
                wav = wav.mean(1)
            if sr != 16000:
                import librosa

                wav = librosa.resample(
                    wav.astype(np.float32), orig_sr=sr, target_sr=16000
                )
                sr = 16000
            pcm = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
            for w in wins_by_clip[name]:
                nm = safe(name, w["win_idx"])
                a0, a1 = int(w["start"] * sr), int(w["end"] * sr)
                seg = pcm[a0:a1]
                path = audio_dir / f"{nm}.wav"
                with wave.open(str(path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframes(seg.tobytes())
                man.write(
                    json.dumps(
                        {
                            "name": nm,
                            "clip": name,
                            "win_idx": w["win_idx"],
                            "split": s,
                            "audio": str(path),
                            "dur": round(len(seg) / sr, 3),
                            "text": w["text"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                split_out[s].append(nm)
                written += 1
        print(f"  {Path(f).name}: written so far={written}", flush=True)
    man.close()
    json.dump(split_out, open(out_dir / "windowed_split.json", "w"), ensure_ascii=False)
    print(
        f"DONE windows materialized={written}  "
        f"train={len(split_out['train'])} val={len(split_out['val'])} "
        f"test_clips={len(split_out['test_clips'])}"
    )


if __name__ == "__main__":
    main()
