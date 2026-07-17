"""Verify a Qwen3-ASR LoRA overfit: transcribe the sample7 clips and compare to ground truth.

Loads the base model + the trained LoRA adapter (PeftModel), transcribes each train clip with
``language=None``, and prints hyp vs ground-truth with char-similarity + an exact-match count and
corpus WER (mirrors the gemma4 ``infer.py`` smoke check).
"""

import argparse
import json
from difflib import SequenceMatcher

import jiwer
import torch
from peft import PeftModel
from qwen_asr import Qwen3ASRModel

_ASR_TAG = "<asr_text>"
_NORM = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Qwen/Qwen3-ASR-0.6B")
    p.add_argument("--adapter", default="/home/qwen3ft/out/sample7-overfit")
    p.add_argument("--train_file", default="/home/qwen3ft/train.jsonl")
    p.add_argument("--max_new_tokens", type=int, default=2048)
    args = p.parse_args()

    wrapper = Qwen3ASRModel.from_pretrained(
        args.model,
        dtype=torch.bfloat16,
        device_map="cuda:0",
        max_new_tokens=args.max_new_tokens,
    )
    if args.adapter:
        wrapper.model = PeftModel.from_pretrained(wrapper.model, args.adapter)
        print(f"Loaded adapter: {args.adapter}", flush=True)

    with open(args.train_file) as f:
        items = [json.loads(line) for line in f if line.strip()]

    refs = [it["text"].split(_ASR_TAG, 1)[-1] for it in items]
    paths = [it["audio"] for it in items]
    results = wrapper.transcribe(paths, language=[None] * len(paths))
    hyps = [r.text for r in results]

    exact = 0
    for it, ref, hyp in zip(items, refs, hyps):
        cs = SequenceMatcher(None, ref.strip(), hyp.strip()).ratio()
        ok = ref.strip() == hyp.strip()
        exact += ok
        name = it["audio"].split("/")[-1]
        print(
            f"\n=== {name}  char-sim={cs:.3f}  exact={ok}  len(ref)={len(ref)} len(hyp)={len(hyp)} ==="
        )
        print(f"  REF: {ref[:200]}")
        print(f"  HYP: {hyp[:200]}")
        print(f"  REF tail: ...{ref[-120:]}")
        print(f"  HYP tail: ...{hyp[-120:]}")

    wer = jiwer.wer(refs, hyps, reference_transform=_NORM, hypothesis_transform=_NORM)
    print(
        f"\nSUMMARY: exact={exact}/{len(items)}  corpus WER={wer*100:.2f}%", flush=True
    )


if __name__ == "__main__":
    main()
