"""Batched WER eval for a Gemma 4 audio LoRA adapter over the full CoSHE dataset.

No-cache generation is slow (gemma4 cache bug), so we BATCH clips through
generate() with left padding to make evaluating ~2000 clips feasible. Reports
corpus WER (the goal metric) + mean per-clip WER.

    python eval_wer.py --adapter /train/out/full --parquet_glob '/coshe-full/data/eval-*.parquet' \
        --cache_path /train/out/coshe_full_cache.pkl --batch_size 16 [--limit N]
"""

import argparse

import jiwer
import torch
from data import DEFAULT_PROMPT, CosheParquetDataset, CosheSample7Dataset
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

_WER_NORM = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--adapter", default="")
    p.add_argument("--parquet_glob", default="")
    p.add_argument("--data_dir", default="/data/coshe-eval/sample7")
    p.add_argument("--cache_path", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max_seconds", type=float, default=30.0)
    p.add_argument("--target_max_chars", type=int, default=0)
    p.add_argument("--max_new_tokens", type=int, default=512)
    p.add_argument("--batch_size", type=int, default=16)
    p.add_argument(
        "--use_cache",
        action="store_true",
        help="use KV cache (needs --patch for gemma4)",
    )
    p.add_argument(
        "--patch", action="store_true", help="apply gemma4 use_cache prefill bugfix"
    )
    return p.parse_args()


def main():
    args = parse_args()
    if args.patch:
        import gemma4_cache_patch

        gemma4_cache_patch.apply()
        print("applied gemma4 use_cache patch", flush=True)
    proc = AutoProcessor.from_pretrained(args.model)
    proc.tokenizer.padding_side = "left"  # left padding for batched generation

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
    model.eval()
    # cached gemma4 multimodal forward is buggy unless --patch is applied
    model.config.use_cache = args.use_cache

    if args.parquet_glob:
        ds = CosheParquetDataset(
            args.parquet_glob,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
            limit=args.limit,
            cache_path=args.cache_path,
        )
    else:
        ds = CosheSample7Dataset(
            args.data_dir,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
        )
    items = list(ds)
    print(f"Evaluating {len(items)} clips, batch_size={args.batch_size}", flush=True)

    refs, hyps, wers = [], [], []
    for start in range(0, len(items), args.batch_size):
        batch = items[start : start + args.batch_size]
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
        in_len = inp["input_ids"].shape[-1]
        with torch.inference_mode():
            out = model.generate(
                **inp,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                use_cache=args.use_cache,
            )
        for i, it in enumerate(batch):
            gen = proc.decode(out[i][in_len:], skip_special_tokens=True).strip()
            refs.append(it["target"])
            hyps.append(gen)
            w = (
                jiwer.wer(
                    it["target"],
                    gen,
                    reference_transform=_WER_NORM,
                    hypothesis_transform=_WER_NORM,
                )
                if it["target"].strip()
                else 0.0
            )
            wers.append(w)
        done = start + len(batch)
        run_corpus = jiwer.wer(
            refs, hyps, reference_transform=_WER_NORM, hypothesis_transform=_WER_NORM
        )
        print(
            f"  {done}/{len(items)}  running corpus WER={run_corpus*100:.2f}%  "
            f"batch mean WER={sum(wers[-len(batch):])/len(batch)*100:.2f}%",
            flush=True,
        )

    corpus = jiwer.wer(
        refs, hyps, reference_transform=_WER_NORM, hypothesis_transform=_WER_NORM
    )
    mean = sum(wers) / len(wers)
    n_exact = sum(1 for w in wers if w == 0.0)
    print(f"\n==== FULL EVAL ====", flush=True)
    print(
        f"clips={len(wers)}  exact(WER=0)={n_exact}  "
        f"mean per-clip WER={mean*100:.2f}%  CORPUS WER={corpus*100:.2f}%",
        flush=True,
    )


if __name__ == "__main__":
    main()
