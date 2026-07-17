"""On the A100/transformers-5.7: is the use_cache bug still present?

A correct implementation gives IDENTICAL logits/loss for a single full forward
regardless of use_cache. We compare use_cache True vs False on one batch with the
current adapter. If equal -> cache is fine -> fast cached generation/eval. If they
differ -> bug persists -> use_cache=False for eval.
"""

import argparse
import time

import torch
from data import DEFAULT_PROMPT, CosheParquetDataset, Gemma4AudioCollator
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

p = argparse.ArgumentParser()
p.add_argument("--adapter", default="/home/gemma4ft/out/full/checkpoint-994")
p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
p.add_argument("--cache_path", default="/home/gemma4ft/out/coshe_full_cache.pkl")
args = p.parse_args()

proc = AutoProcessor.from_pretrained("google/gemma-4-E2B-it")
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
    "google/gemma-4-E2B-it",
    quantization_config=bnb,
    dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)
model = PeftModel.from_pretrained(model, args.adapter)
model.eval()

ds = CosheParquetDataset(
    args.parquet_glob,
    max_seconds=30.0,
    target_max_chars=0,
    cache_path=args.cache_path,
    limit=8,
)
col = Gemma4AudioCollator(proc)
b = col([ds[0]])
b = {k: v.to(model.device) for k, v in b.items()}

print("=== single-forward loss: use_cache True vs False ===", flush=True)
for uc in [False, True]:
    with torch.inference_mode():
        loss = float(model(**b, use_cache=uc).loss)
    print(f"  use_cache={uc}: loss={loss:.4f}", flush=True)

print("\n=== generation speed: cache vs no-cache (4 clips) ===", flush=True)
proc.tokenizer.padding_side = "left"
texts, audios = [], []
for it in [ds[i] for i in range(4)]:
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
        proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    )
    audios.append(it["audio"])
inp = proc(text=texts, audio=audios, return_tensors="pt", padding=True).to(model.device)
for uc in [True, False]:
    t = time.time()
    with torch.inference_mode():
        out = model.generate(**inp, max_new_tokens=200, do_sample=False, use_cache=uc)
    dt = time.time() - t
    txt = proc.decode(out[0][inp["input_ids"].shape[-1] :], skip_special_tokens=True)[
        :120
    ]
    print(f"  use_cache={uc}: {dt:.1f}s for 4 clips | sample: {txt!r}", flush=True)
