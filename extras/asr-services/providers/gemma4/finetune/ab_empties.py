"""A/B diagnostic for the 12B empty-output / bail behavior on CoSHE.

Runs the first N deterministic clips under several decode configs, prints
GT vs HYP, and dumps the RAW (pre-strip) output for empties so we can see
exactly what the model emits (immediate EOS? thinking-only? refusal?).
"""

import glob
import os
import re
import sys

sys.path.insert(0, "/home/gemma4ft")
import pyarrow.parquet as pq
import torch
from bench_coshe_12b import SR, VERBATIM_PROMPT, decode_audio, select_names
from transformers import AutoModelForMultimodalLM, AutoProcessor

MODEL = "google/gemma-4-12B-it"
DATA = "/home/coshe-data/data"
N = 25

proc = AutoProcessor.from_pretrained(MODEL)
model = AutoModelForMultimodalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="auto"
).eval()
print("model loaded\n", flush=True)

sel, shards = select_names(DATA, 500, "coshe-eval")
clips = []
for s in shards:
    t = pq.read_table(s, columns=["audio_file_name", "transcription", "audio"])
    for nm, tr, au in zip(
        t.column(0).to_pylist(), t.column(1).to_pylist(), t.column(2).to_pylist()
    ):
        if nm in sel and au and au.get("bytes"):
            clips.append((nm, decode_audio(au["bytes"]), tr))
            if len(clips) >= N:
                break
    if len(clips) >= N:
        break


def strip_ch(raw):
    parts = []
    for part in raw.split("<channel|>"):
        parts.append(part.split("<|channel>")[0] if "<|channel>" in part else part)
    t = "".join(parts)
    t = re.sub(r"<\|?[a-z_]+\|?>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def gen(audio, enable_thinking, min_new):
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VERBATIM_PROMPT},
                {"type": "audio", "audio": audio},
            ],
        }
    ]
    inp = proc.apply_chat_template(
        msgs,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
        add_generation_prompt=True,
        enable_thinking=enable_thinking,
    ).to(model.device)
    il = inp["input_ids"].shape[-1]
    kw = dict(max_new_tokens=1024, do_sample=False, no_repeat_ngram_size=3)
    if min_new:
        kw["min_new_tokens"] = min_new
    with torch.inference_mode():
        out = model.generate(**inp, **kw)
    raw = proc.decode(out[0][il:], skip_special_tokens=False)
    return raw, strip_ch(raw)


CONFIGS = [
    ("A: think=False min0  (current)", False, 0),
    ("B: think=False min32 (force gen)", False, 32),
    ("C: think=True  min0  (real think)", True, 0),
]
for label, et, mn in CONFIGS:
    empt = 0
    print(f"\n########## CONFIG {label} ##########", flush=True)
    for nm, audio, gt in clips:
        raw, hyp = gen(audio, et, mn)
        if not hyp:
            empt += 1
            print(f"[{nm} {len(audio)/SR:.0f}s] EMPTY  RAW={raw[:140]!r}", flush=True)
        else:
            print(f"[{nm} {len(audio)/SR:.0f}s] GT : {gt[:80]}", flush=True)
            print(f"{' '*len(nm)}        HYP: {hyp[:80]}", flush=True)
    print(f"==> {label}: empty={empt}/{len(clips)}", flush=True)
