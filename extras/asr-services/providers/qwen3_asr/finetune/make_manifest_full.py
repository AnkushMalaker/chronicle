"""Build the FULL CoSHE train.jsonl for the <2% WER overfit (runs on the A100).

Decodes every clip in the parquet shards to a 16 kHz mono WAV under ``--audio_dir`` and emits
a JSONL in the official ``qwen3_asr_sft.py`` schema:

    {"audio": "/abs/path.wav", "text": "language None<asr_text>" + <ground-truth transcript>}

~1985 clips → a few GB of WAVs. Resumable: skips clips whose WAV already exists. Mirrors the
gemma4 CosheParquetDataset coverage (all shards, full transcripts).
"""

import argparse
import io
import json
from glob import glob
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    p.add_argument("--audio_dir", default="/home/qwen3ft/coshe_audio")
    p.add_argument("--out", default="/home/qwen3ft/train_full.jsonl")
    p.add_argument("--prefix", default="language None<asr_text>")
    p.add_argument("--limit", type=int, default=0, help="cap #clips (0 = all)")
    args = p.parse_args()

    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(args.out, "w") as out_f:
        for shard in sorted(glob(args.parquet_glob)):
            pf = pq.ParquetFile(shard)
            for rb in pf.iter_batches(batch_size=64):
                for row in rb.to_pylist():
                    if args.limit and n >= args.limit:
                        break
                    name = row["audio_file_name"]
                    wav_path = audio_dir / name
                    if not wav_path.exists():
                        data, sr = sf.read(
                            io.BytesIO(row["audio"]["bytes"]), dtype="float32"
                        )
                        if data.ndim > 1:
                            data = data.mean(axis=1)
                        sf.write(str(wav_path), np.ascontiguousarray(data), sr)
                    out_f.write(
                        json.dumps(
                            {
                                "audio": str(wav_path),
                                "text": args.prefix + row["transcription"],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    n += 1
                if args.limit and n >= args.limit:
                    break
            if args.limit and n >= args.limit:
                break
    print(f"Wrote {n} examples -> {args.out}; audio in {audio_dir}")


if __name__ == "__main__":
    main()
