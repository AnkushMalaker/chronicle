"""Build NeMo manifests + 16 kHz mono WAVs from the CoSHE-Eval parquet shards.

Nemotron-3.5-asr-streaming is ``EncDecRNNTBPEModelWithPrompt`` (NeMo 2.8.0rc0): a
cache-aware FastConformer-RNNT whose Lhotse dataloader reads a per-utterance
language from the manifest ``prompt_field`` (``target_lang``) and maps it through
the model's ``prompt_dictionary`` (hi-IN -> 6, en-US -> 0, ...). So every manifest
line needs ``audio_filepath``, ``text``, ``duration`` and ``target_lang``.

This mirrors bench_coshe.decode_to_wav16k so train/eval audio is byte-identical to
the benchmark. It powers all three FT stages:

  Stage 1 (overfit smoke):   --indices 0 1                 (1-2 clips)
  Stage 2 (full overfit):    --all                         (1985 clips, one split)
  Stage 3 (generalization):  --split 0.2 --seed 0          (train/val/test jsonls)

Usage:
    python make_manifest.py --dataset /home/coshe/data --out-dir /home/ft/data \
        --target-lang hi-IN --indices 0 1
    python make_manifest.py --dataset /home/coshe/data --out-dir /home/ft/data \
        --target-lang hi-IN --split 0.2 --seed 0
"""

import argparse
import io
import json
import random
from pathlib import Path

import librosa
import pyarrow.parquet as pq
import soundfile as sf


def decode_to_wav16k(audio_bytes: bytes, dst: str) -> float:
    """Decode embedded audio bytes -> 16 kHz mono PCM16 WAV. Returns duration (s).

    Identical to bench_coshe.decode_to_wav16k so FT audio matches the benchmark.
    """
    data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        data = librosa.resample(data, orig_sr=sr, target_sr=16000)
        sr = 16000
    sf.write(dst, data, sr, subtype="PCM_16")
    return len(data) / sr


def iter_rows(dataset: str):
    """Yield (global_index, row_dict) over all eval-*.parquet shards in order."""
    shards = sorted(Path(dataset).glob("eval-*.parquet"))
    if not shards:
        raise SystemExit(f"No eval-*.parquet shards under {dataset}")
    gi = 0
    for shard in shards:
        pf = pq.ParquetFile(shard)
        for batch in pf.iter_batches(batch_size=64):
            for row in batch.to_pylist():
                yield gi, row
                gi += 1


def write_manifest(rows, wav_dir: Path, manifest_path: Path, target_lang: str) -> int:
    wav_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(manifest_path, "w") as mf:
        for _gi, row in rows:
            name = row["audio_file_name"]
            wav_path = wav_dir / f"{Path(name).stem}.wav"
            dur = decode_to_wav16k(row["audio"]["bytes"], str(wav_path))
            mf.write(
                json.dumps(
                    {
                        "audio_filepath": str(wav_path),
                        "text": row["transcription"],
                        "duration": round(dur, 3),
                        "target_lang": target_lang,
                        "audio_file_name": name,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            n += 1
    print(f"  wrote {n} -> {manifest_path}", flush=True)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="/home/coshe/data")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--target-lang",
        default="hi-IN",
        help="prompt_dictionary key written to every manifest line",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--indices", type=int, nargs="+", help="explicit global row indices")
    g.add_argument("--names", nargs="+", help="explicit audio_file_name values")
    g.add_argument("--all", action="store_true", help="every clip into one manifest")
    g.add_argument(
        "--split", type=float, help="held-out fraction, e.g. 0.2 (train=this frac)"
    )
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out_dir)
    wav_dir = out / "wav"

    if args.indices is not None:
        want = set(args.indices)
        rows = [(gi, r) for gi, r in iter_rows(args.dataset) if gi in want]
        write_manifest(rows, wav_dir, out / "overfit.json", args.target_lang)

    elif args.names is not None:
        want = set(args.names)
        rows = [
            (gi, r) for gi, r in iter_rows(args.dataset) if r["audio_file_name"] in want
        ]
        got = {r["audio_file_name"] for _gi, r in rows}
        missing = want - got
        if missing:
            raise SystemExit(f"names not found: {sorted(missing)}")
        write_manifest(rows, wav_dir, out / "overfit.json", args.target_lang)

    elif args.all:
        rows = list(iter_rows(args.dataset))
        write_manifest(rows, wav_dir, out / "all.json", args.target_lang)

    else:  # --split: train = args.split fraction, then half of remainder = val, half = test
        all_rows = list(iter_rows(args.dataset))
        idx = list(range(len(all_rows)))
        random.Random(args.seed).shuffle(idx)
        n = len(idx)
        n_train = int(round(args.split * n))
        n_val = (n - n_train) // 2
        train_i = set(idx[:n_train])
        val_i = set(idx[n_train : n_train + n_val])
        test_i = set(idx[n_train + n_val :])
        print(
            f"split seed={args.seed}: train={len(train_i)} val={len(val_i)} test={len(test_i)}",
            flush=True,
        )
        write_manifest(
            [(gi, r) for gi, r in enumerate(all_rows) if gi in train_i],
            wav_dir,
            out / "train.json",
            args.target_lang,
        )
        write_manifest(
            [(gi, r) for gi, r in enumerate(all_rows) if gi in val_i],
            wav_dir,
            out / "val.json",
            args.target_lang,
        )
        write_manifest(
            [(gi, r) for gi, r in enumerate(all_rows) if gi in test_i],
            wav_dir,
            out / "test.json",
            args.target_lang,
        )


if __name__ == "__main__":
    main()
