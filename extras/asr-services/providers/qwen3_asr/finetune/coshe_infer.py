"""Transcribe the CoSHE-Eval dataset with a Qwen3-ASR model and emit mlexp-schema JSONL.

Runs on the A100 (qwen-asr installed). Reads the ``eval-*.parquet`` shards, decodes each
row's audio bytes, batch-transcribes with ``Qwen3ASRModel`` (``language=None`` → language-
agnostic), and appends one JSONL line per clip in the exact schema mlexp's ``score_coshe``
expects:

    {"audio_file_name", "transcription" (ground truth), "hyp", "asr_seconds",
     "duration_s", "raw"}

Resumable: rows whose ``audio_file_name`` already has a non-error line in the output are
skipped (mirrors ``mlexp.runners.dataset.load_done``). WER scoring (IndicXlit romanization +
jiwer) happens locally back in extras/ml-experiments, not here — this script only produces
hypotheses.
"""

import argparse
import io
import json
import time
from glob import glob
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
from qwen_asr import Qwen3ASRModel


def load_done(out_path: Path) -> set[str]:
    done: set[str] = set()
    if not out_path.exists():
        return done
    with open(out_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if "error" in rec:
                continue
            name = rec.get("audio_file_name")
            if name:
                done.add(name)
    return done


def decode_audio(raw_bytes: bytes) -> tuple[np.ndarray, int]:
    """WAV/audio bytes -> (float32 mono, sr). qwen_asr handles resampling internally."""
    data, sr = sf.read(io.BytesIO(raw_bytes), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return np.ascontiguousarray(data), int(sr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--model", required=True, help="HF id or local path of the Qwen3-ASR model"
    )
    p.add_argument("--out", required=True, help="output JSONL path")
    p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    p.add_argument(
        "--limit", type=int, default=0, help="cap #clips (shard order); 0 = all"
    )
    p.add_argument("--batch_size", type=int, default=16)
    p.add_argument("--max_new_tokens", type=int, default=1024)
    args = p.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out_path)
    print(f"Model: {args.model}", flush=True)
    print(f"Out:   {out_path}  (resuming, {len(done)} already done)", flush=True)

    import torch

    model = Qwen3ASRModel.from_pretrained(
        args.model,
        dtype=torch.bfloat16,
        device_map="cuda:0",
        max_new_tokens=args.max_new_tokens,
        max_inference_batch_size=args.batch_size,
    )

    shards = sorted(glob(args.parquet_glob))
    n_ok = n_err = n_seen = 0
    t_start = time.time()

    def flush_batch(batch, out_f):
        nonlocal n_ok, n_err
        if not batch:
            return
        audios = [b["audio"] for b in batch]
        try:
            t0 = time.time()
            results = model.transcribe(audios, language=[None] * len(audios))
            dt = (time.time() - t0) / len(audios)
        except Exception as e:  # batch-level failure -> record per-clip errors
            for b in batch:
                out_f.write(
                    json.dumps(
                        {"audio_file_name": b["name"], "error": str(e)},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                n_err += 1
            out_f.flush()
            return
        for b, res in zip(batch, results):
            rec = {
                "audio_file_name": b["name"],
                "transcription": b["gt"],
                "hyp": res.text,
                "asr_seconds": round(dt, 2),
                "duration_s": b["duration_s"],
                "raw": {"text": res.text, "language": getattr(res, "language", None)},
            }
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += 1
        out_f.flush()

    with open(out_path, "a") as out_f:
        batch = []
        for shard in shards:
            pf = pq.ParquetFile(shard)
            for rb in pf.iter_batches(batch_size=64):
                for row in rb.to_pylist():
                    if args.limit and n_seen >= args.limit:
                        break
                    name = row["audio_file_name"]
                    n_seen += 1
                    if name in done:
                        continue
                    audio, sr = decode_audio(row["audio"]["bytes"])
                    batch.append(
                        {
                            "name": name,
                            "gt": row["transcription"],
                            "audio": (audio, sr),
                            "duration_s": round(len(audio) / sr, 2),
                        }
                    )
                    if len(batch) >= args.batch_size:
                        flush_batch(batch, out_f)
                        batch = []
                        if n_ok % 64 == 0:
                            el = time.time() - t_start
                            print(
                                f"  done={n_ok} err={n_err} seen={n_seen} "
                                f"({el:.0f}s, {n_ok / max(el, 1):.2f} clip/s)",
                                flush=True,
                            )
                if args.limit and n_seen >= args.limit:
                    break
            if args.limit and n_seen >= args.limit:
                break
        flush_batch(batch, out_f)

    print(
        f"DONE: ok={n_ok} err={n_err} seen={n_seen} elapsed={time.time() - t_start:.0f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
