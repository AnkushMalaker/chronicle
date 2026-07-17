"""Build the sample7 overfit train.jsonl for Qwen3-ASR SFT (runs on the A100).

Extracts the 7 CoSHE sample7 clips from the parquet shards by ``audio_file_name``, writes
each as a 16 kHz mono WAV, and emits a JSONL in the schema the official ``qwen3_asr_sft.py``
expects:

    {"audio": "/abs/path.wav", "text": "language None<asr_text>" + <ground-truth transcript>}

The ``language None`` prefix matches how we evaluate (``transcribe(language=None)``); WER is
computed on the parsed ``<asr_text>`` payload, so the language token itself is irrelevant.
"""

import argparse
import io
import json
from glob import glob
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

SAMPLE7 = [
    "audio_13.wav",
    "audio_278.wav",
    "audio_1320.wav",
    "audio_58.wav",
    "audio_1197.wav",
    "audio_1149.wav",
    "audio_1059.wav",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    p.add_argument("--audio_dir", default="/home/qwen3ft/sample7_audio")
    p.add_argument("--out", default="/home/qwen3ft/train.jsonl")
    p.add_argument("--prefix", default="language None<asr_text>")
    args = p.parse_args()

    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    want = set(SAMPLE7)
    found: dict[str, dict] = {}

    for shard in sorted(glob(args.parquet_glob)):
        if len(found) == len(want):
            break
        pf = pq.ParquetFile(shard)
        for rb in pf.iter_batches(batch_size=64):
            for row in rb.to_pylist():
                name = row["audio_file_name"]
                if name not in want or name in found:
                    continue
                data, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
                if data.ndim > 1:
                    data = data.mean(axis=1)
                wav_path = audio_dir / name
                sf.write(str(wav_path), np.ascontiguousarray(data), sr)
                found[name] = {
                    "audio": str(wav_path),
                    "text": args.prefix + row["transcription"],
                }
            if len(found) == len(want):
                break

    missing = want - set(found)
    if missing:
        raise SystemExit(f"Missing clips not found in parquet: {sorted(missing)}")

    with open(args.out, "w") as f:
        for name in SAMPLE7:
            f.write(json.dumps(found[name], ensure_ascii=False) + "\n")
    print(f"Wrote {len(found)} examples -> {args.out}")
    print(f"Audio in {audio_dir}")


if __name__ == "__main__":
    main()
