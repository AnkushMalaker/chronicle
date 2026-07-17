"""Evaluate a fine-tuned nemotron .nemo checkpoint on a CoSHE manifest.

Loads a saved .nemo (restore_from), transcribes every clip in the manifest, and
writes one JSONL line per clip in the schema mlexp score_coshe.py consumes
({audio_file_name, transcription, hyp, asr_seconds, duration_s}). Score locally:

    uv run --with "jiwer,requests,tqdm" python3 \
        extras/ml-experiments/src/mlexp/evaluate/score_coshe.py \
        --result ft=ft.jsonl --result base=nemotron_auto_full.jsonl --out-dir report/

Resumable: clips already present in --out are skipped.

Usage:
    python eval_full.py --init /home/ft/out/warm/final.nemo \
        --manifest /home/ft/data_full/all.json --out /home/ft/results/ft_warm.jsonl
"""

import argparse
import json
import time
from pathlib import Path

import nemo.collections.asr as nemo_asr
import torch
from nemo.collections.asr.data.audio_to_text_lhotse_prompt_index import (
    LhotseSpeechToTextBpeDatasetWithPromptIndex,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", required=True, help="path to fine-tuned .nemo")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-lang", default="hi-IN")
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="override RNN-T greedy max_symbols per frame (default cfg=10)",
    )
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.manifest)]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        for l in open(out_path):
            try:
                done.add(json.loads(l)["audio_file_name"])
            except Exception:
                pass
    todo = [r for r in rows if r["audio_file_name"] not in done]
    print(f"{len(done)} done, {len(todo)} to eval", flush=True)

    # --init can be a local .nemo (fine-tuned) OR a HF model id (base, for comparison)
    if Path(args.init).is_file():
        model = nemo_asr.models.ASRModel.restore_from(args.init)
    else:
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=args.init)
    model.eval()

    # RNN-T greedy decode caps at `max_symbols` tokens per encoder frame (default 10);
    # too low truncates long / token-dense (Devanagari) segments. Bump to test.
    if args.max_symbols:
        from omegaconf import open_dict

        dec = model.cfg.decoding
        with open_dict(dec):
            dec.strategy = "greedy_batch"
            dec.greedy.max_symbols = args.max_symbols
        model.change_decoding_strategy(dec)

    # force the training target_lang prompt; num_workers=0 so this in-process
    # override applies (worker subprocesses wouldn't see it). See train_overfit.py.
    LhotseSpeechToTextBpeDatasetWithPromptIndex._get_prompt_index_for_cut = (
        lambda self, cut, _tl=args.target_lang: self._get_prompt_index(_tl)
    )

    t0 = time.time()
    with open(out_path, "a") as out_f, torch.no_grad():
        for i in range(0, len(todo), args.batch_size):
            batch = todo[i : i + args.batch_size]
            wavs = [r["audio_filepath"] for r in batch]
            t1 = time.time()
            hyps = model.transcribe(
                wavs,
                batch_size=args.batch_size,
                target_lang=args.target_lang,
                num_workers=0,
                verbose=False,
            )
            dt = (time.time() - t1) / len(batch)
            for r, h in zip(batch, hyps):
                text = h.text if hasattr(h, "text") else str(h)
                out_f.write(
                    json.dumps(
                        {
                            "audio_file_name": r["audio_file_name"],
                            "transcription": r["text"],
                            "hyp": text,
                            "asr_seconds": round(dt, 3),
                            "duration_s": r.get("duration", 0),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                out_f.flush()
            n = i + len(batch)
            if n % 50 == 0 or n == len(todo):
                el = time.time() - t0
                print(f"  {n}/{len(todo)}  ({el:.0f}s, {n/el:.2f} clip/s)", flush=True)
    print(f"DONE -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
