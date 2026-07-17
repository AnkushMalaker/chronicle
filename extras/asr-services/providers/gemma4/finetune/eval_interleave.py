"""Evaluate base or FT Gemma4-E2B on held-out clips for the interleave audio->pasted_text task.
Per test clip: feed all its <=28s chunks in one app-aware prompt (onestep, bf16, sdpa),
generate the formatted text in one shot, score against pasted_text (the target) with jiwer.
Same prompt/decode for base and adapter -> controlled delta.
"""

import argparse
import json
import re
import statistics

import jiwer
import torch
from data_interleave import _load_wav_f32, build_prompt
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor

_B = re.compile(r"\[[^\]]*\]")
_A = re.compile(r"['’`]")
_P = re.compile(r"[^\w\s]")
_W = re.compile(r"\s+")


def norm(t):
    t = _B.sub(" ", t).lower()
    t = _A.sub("", t)
    t = _P.sub(" ", t)
    return _W.sub(" ", t).strip()


@torch.inference_mode()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-E2B-it")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--manifest", default="/home/wispr_interleave/manifest.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max_new_tokens", type=int, default=512)
    args = ap.parse_args()

    test = [
        json.loads(l) for l in open(args.manifest) if json.loads(l)["split"] == "test"
    ]
    proc = AutoProcessor.from_pretrained(args.model)
    proc.tokenizer.padding_side = "left"
    model = AutoModelForMultimodalLM.from_pretrained(
        args.model, dtype=torch.bfloat16, device_map="auto"
    )  # default attn (sdpa), bf16
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
        print(f"adapter: {args.adapter}", flush=True)
    model.eval()
    model.config.use_cache = True

    rows = []
    fout = open(args.out, "w")
    for ci, r in enumerate(test):
        chunks = [_load_wav_f32(p) for p in r["chunks"]]
        content = [{"type": "text", "text": build_prompt(r.get("app", ""))}]
        content += [{"type": "audio", "audio": a} for a in chunks]
        inp = proc.apply_chat_template(
            [{"role": "user", "content": content}],
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
            enable_thinking=False,
        ).to(model.device)
        g = model.generate(
            **inp, max_new_tokens=args.max_new_tokens, do_sample=False, use_cache=True
        )
        hyp = proc.decode(
            g[0][inp["input_ids"].shape[-1] :], skip_special_tokens=True
        ).strip()
        ref = r["target"]
        rn, hn = norm(ref), norm(hyp)
        wer = jiwer.wer(rn, hn) if hn else 1.0
        rows.append(
            {
                "clip": r["clip"],
                "app": r.get("app", ""),
                "wer": round(wer, 4),
                "ref_words": len(rn.split()),
                "n_chunks": len(chunks),
                "transcription": ref,
                "hyp": hyp,
            }
        )
        fout.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
        fout.flush()
        print(f"[{ci+1}/{len(test)}] {r['clip']} wer={wer:.3f}", flush=True)
    fout.close()

    refs_n = [norm(r["transcription"]) for r in rows]
    hyps_n = [norm(r["hyp"]) for r in rows]
    summary = {
        "adapter": args.adapter or "base",
        "clips": len(rows),
        "corpus_wer": round(jiwer.wer(refs_n, hyps_n), 4),
        "median_wer": round(statistics.median(r["wer"] for r in rows), 4),
    }
    print("SUMMARY " + json.dumps(summary), flush=True)
    json.dump(summary, open(args.out.replace(".jsonl", "_summary.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
