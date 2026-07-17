"""Verify a Gemma 4 audio LoRA adapter by transcribing the training clips and
comparing to ground truth (overfit check).

Loads the 4-bit base + adapter, generates on each sample7 clip, prints the
output next to the manifest target, and reports a crude char-level similarity
(SequenceMatcher ratio) so we can see the loss-near-zero overfit reflected in
the actual decoded text.

Usage:
    python infer.py --model google/gemma-4-E2B-it \
        --adapter /train/out/e2b-overfit --data_dir /data/coshe-eval/sample7
"""

import argparse
from difflib import SequenceMatcher

import jiwer
import torch
from data import DEFAULT_PROMPT, CosheParquetDataset, CosheSample7Dataset
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

# WER normalization: lowercase, strip punctuation, collapse whitespace. For an
# overfit, the model is trained on the target's exact script, so exact
# reproduction -> WER 0 (no romanization needed; raw mixed-script compares fine
# when hyp and ref use the same script, which they do after training).
_WER_NORM = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def compute_wer(ref: str, hyp: str) -> float:
    if not ref.strip():
        return 0.0 if not hyp.strip() else 1.0
    return jiwer.wer(
        ref, hyp, reference_transform=_WER_NORM, hypothesis_transform=_WER_NORM
    )


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--adapter", default="")
    p.add_argument("--data_dir", default="/data/coshe-eval/sample7")
    p.add_argument("--parquet_glob", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--cache_path", default="")
    p.add_argument("--max_seconds", type=float, default=30.0)
    p.add_argument("--target_max_chars", type=int, default=0)
    p.add_argument("--max_new_tokens", type=int, default=512)
    return p.parse_args()


def main():
    args = parse_args()
    processor = AutoProcessor.from_pretrained(args.model)
    processor.tokenizer.padding_side = "left"

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
    tag = "BASE (no adapter)"
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
        tag = f"ADAPTER {args.adapter}"
    model.eval()
    # CRITICAL: gemma4's multimodal forward produces WRONG audio-conditioned logits
    # when the KV cache is enabled (transformers 5.5.0) — with use_cache=True a
    # fully-overfit adapter scores loss ~4.5 (≈ base) instead of ~0.1, and
    # generation reverts to near-base output. Generate with the cache OFF.
    model.config.use_cache = False

    if args.parquet_glob:
        dataset = CosheParquetDataset(
            args.parquet_glob,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
            limit=args.limit,
            cache_path=args.cache_path,
        )
    else:
        dataset = CosheSample7Dataset(
            args.data_dir,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
        )
    print(f"\n===== INFERENCE: {tag} =====", flush=True)
    ratios = []
    wers = []
    all_gens = []
    for item in dataset:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": DEFAULT_PROMPT},
                    {"type": "audio", "audio": item["audio"]},
                ],
            }
        ]
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
        ).to(model.device)
        in_len = inputs["input_ids"].shape[-1]
        with torch.inference_mode():
            out = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                use_cache=False,
            )
        gen = processor.decode(out[0][in_len:], skip_special_tokens=True).strip()
        target = item["target"]
        ratio = SequenceMatcher(None, gen, target).ratio()
        wer = compute_wer(target, gen)
        ratios.append(ratio)
        wers.append(wer)
        all_gens.append(gen)
        print(
            f"\n--- {item['name']}  (char-sim {ratio:.3f}  WER {wer*100:.2f}%) ---",
            flush=True,
        )
        print(f"  TARGET: {target[:240]}", flush=True)
        print(f"  OUTPUT: {gen[:240]}", flush=True)

    mean_wer = sum(wers) / len(wers)
    # corpus WER (aggregate over all words), the headline metric for the goal
    corpus_wer = jiwer.wer(
        [d["target"] for d in dataset],
        all_gens,
        reference_transform=_WER_NORM,
        hypothesis_transform=_WER_NORM,
    )
    print(f"\nMean char-similarity: {sum(ratios)/len(ratios):.3f}", flush=True)
    print(
        f"Mean per-clip WER: {mean_wer*100:.2f}%   Corpus WER: {corpus_wer*100:.2f}%",
        flush=True,
    )


if __name__ == "__main__":
    main()
