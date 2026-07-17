"""Transcribe the held-out CoSHE test split with the base model and/or a LoRA adapter,
emitting mlexp-schema JSONL for scoring.

Using the SAME harness for base and adapters means the base-vs-FT WER delta isolates the
adapter's effect (no service / audio-handling confound). Audio is the cached 30s window
(same as training); the same window + decode settings are applied to every model, so the
delta is controlled even though the absolute WER carries the 30s-truncation tail equally.
"""

import argparse
import json
import os
import random
import time

import torch
from data import DEFAULT_PROMPT, CosheParquetDataset
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig
from window_target import apply_window_truncation


def load_done(path):
    done = set()
    if os.path.exists(path):
        for line in open(path):
            try:
                done.add(json.loads(line)["audio_file_name"])
            except Exception:
                pass
    return done


@torch.inference_mode()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-E4B-it")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--split_file", default="/home/gemma4ft/split_20_10_70.json")
    ap.add_argument("--cache_path", default="/home/gemma4ft/out/coshe_full_cache.pkl")
    ap.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch_size", type=int, default=8)
    ap.add_argument("--max_new_tokens", type=int, default=1024)
    ap.add_argument("--limit", type=int, default=0, help="cap #clips (smoke test)")
    ap.add_argument(
        "--window_seconds",
        type=float,
        default=30.0,
        help="proportionally truncate ref to this audio window (0=full)",
    )
    ap.add_argument("--durations", default="/home/gemma4ft/durations.json")
    args = ap.parse_args()

    proc = AutoProcessor.from_pretrained(args.model)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        llm_int8_skip_modules=[
            "model.audio_tower",
            "model.vision_tower",
            "model.embed_audio",
            "model.embed_vision",
            "lm_head",
        ],
    )
    model = AutoModelForMultimodalLM.from_pretrained(
        args.model,
        quantization_config=bnb,
        dtype=torch.bfloat16,
        device_map="auto",
        attn_implementation="eager",
    )
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
        print(f"loaded adapter: {args.adapter}", flush=True)
    model.eval()
    model.config.use_cache = True
    proc.tokenizer.padding_side = "left"

    split = json.load(open(args.split_file))
    test_names = split["test"]
    ds = CosheParquetDataset(
        args.parquet_glob, max_seconds=30.0, cache_path=args.cache_path
    )
    apply_window_truncation(list(ds), args.durations, args.window_seconds)
    by = {it["name"]: it for it in ds}
    items = [by[n] for n in test_names if n in by]
    if args.limit and args.limit < len(items):
        # seeded random subset so every model is evaluated on the SAME clips
        items = sorted(
            random.Random(0).sample(items, args.limit), key=lambda it: it["name"]
        )
    done = load_done(args.out)
    items = [it for it in items if it["name"] not in done]
    print(
        f"test split={len(test_names)}  to_do={len(items)}  already_done={len(done)}",
        flush=True,
    )

    fout = open(args.out, "a")
    t0 = time.time()
    for s in range(0, len(items), args.batch_size):
        batch = items[s : s + args.batch_size]
        texts, audios = [], []
        for it in batch:
            msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_PROMPT},
                        {"type": "audio", "audio": it["audio"]},
                    ],
                }
            ]
            texts.append(
                proc.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=True
                )
            )
            audios.append(it["audio"])
        inp = proc(text=texts, audio=audios, return_tensors="pt", padding=True).to(
            model.device
        )
        ts = time.time()
        out = model.generate(
            **inp, max_new_tokens=args.max_new_tokens, do_sample=False, use_cache=True
        )
        dt = time.time() - ts
        for i, it in enumerate(batch):
            hyp = proc.decode(
                out[i][inp["input_ids"].shape[-1] :], skip_special_tokens=True
            ).strip()
            rec = {
                "audio_file_name": it["name"],
                "transcription": it["target"],
                "hyp": hyp,
                "asr_seconds": round(dt / len(batch), 3),
                "duration_s": round(len(it["audio"]) / 16000.0, 2),
                "raw": hyp,
            }
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        print(f"[{s + len(batch)}/{len(items)}] {time.time() - t0:.0f}s", flush=True)
    fout.close()
    print(f"DONE out={args.out}", flush=True)


if __name__ == "__main__":
    main()
