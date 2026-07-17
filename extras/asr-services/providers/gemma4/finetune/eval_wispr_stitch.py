"""Evaluate base or LoRA-adapter Gemma4-E2B on the held-out Wispr test clips.

Test clips are pre-sliced into the SAME fa windows used for training (manifest split=="test").
Each window is transcribed with WISPR_PROMPT via the prod onestep path
(apply_chat_template(tokenize=True, return_dict=True), bf16, model-default attn=sdpa,
use_cache=True), then window hyps are concatenated per clip (non-overlapping -> plain concat)
into a full-clip hypothesis scored against asr_text (test_refs.jsonl) with jiwer.

Same windowing + prompt + decode for base and every adapter -> the base->FT WER delta is
controlled. Run once with --adapter "" (base) and once with --adapter <dir> (FT).

Usage:
    python eval_wispr_stitch.py --manifest .../manifest.jsonl --refs .../test_refs.jsonl \
        --adapter "" --out base.jsonl
"""

import argparse
import json
import re
import statistics
import time
from collections import defaultdict

import jiwer
import torch
from data_wispr import WISPR_PROMPT
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor

_BRACKET = re.compile(r"\[[^\]]*\]")
_APOS = re.compile(r"['’`]")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize(t: str) -> str:
    t = _BRACKET.sub(" ", t).lower()
    t = _APOS.sub("", t)
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


@torch.inference_mode()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-E2B-it")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--manifest", default="/home/wispr_windowed/manifest.jsonl")
    ap.add_argument("--refs", default="/home/wispr_windowed/test_refs.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max_new_tokens", type=int, default=256)
    args = ap.parse_args()

    # test windows grouped by clip
    wins_by_clip = defaultdict(list)
    for line in open(args.manifest):
        r = json.loads(line)
        if r["split"] == "test":
            wins_by_clip[r["clip"]].append(r)
    for c in wins_by_clip:
        wins_by_clip[c].sort(key=lambda w: w["win_idx"])
    refs = {
        json.loads(l)["audio_file_name"]: json.loads(l)["transcription"]
        for l in open(args.refs)
    }

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

    gen_kw = dict(max_new_tokens=args.max_new_tokens, do_sample=False, use_cache=True)
    t0 = time.time()
    rows = []
    fout = open(args.out, "w")
    for ci, (clip, wins) in enumerate(sorted(wins_by_clip.items())):
        parts = []
        for w in wins:
            msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": WISPR_PROMPT},
                        {"type": "audio", "audio": w["audio"]},
                    ],
                }
            ]
            inp = proc.apply_chat_template(
                msgs,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                add_generation_prompt=True,
                enable_thinking=False,
            ).to(model.device)
            out = model.generate(**inp, **gen_kw)
            parts.append(
                proc.decode(
                    out[0][inp["input_ids"].shape[-1] :], skip_special_tokens=True
                ).strip()
            )
        hyp = " ".join(parts).strip()
        ref = refs[clip]
        rn, hn = normalize(ref), normalize(hyp)
        wer = jiwer.wer(rn, hn) if hn else 1.0
        rows.append(
            {
                "audio_file_name": clip,
                "wer": round(wer, 4),
                "ref_words": len(rn.split()),
                "n_wins": len(wins),
                "transcription": ref,
                "hyp": hyp,
            }
        )
        fout.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
        fout.flush()
        print(
            f"[{ci+1}/{len(wins_by_clip)}] {clip} wer={wer:.3f} ({time.time()-t0:.0f}s)",
            flush=True,
        )
    fout.close()

    # corpus + median
    tot_ref = sum(r["ref_words"] for r in rows)
    refs_n = [normalize(refs[r["audio_file_name"]]) for r in rows]
    hyps_n = [normalize(r["hyp"]) for r in rows]
    corpus = jiwer.wer(refs_n, hyps_n)
    median = statistics.median(r["wer"] for r in rows)
    summary = {
        "adapter": args.adapter or "base",
        "clips": len(rows),
        "corpus_wer": round(corpus, 4),
        "median_wer": round(median, 4),
        "total_ref_words": tot_ref,
    }
    print("SUMMARY " + json.dumps(summary), flush=True)
    json.dump(summary, open(args.out.replace(".jsonl", "_summary.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
