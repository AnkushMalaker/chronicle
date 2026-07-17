"""Smoke test the multi-audio interleave collator + a forward pass BEFORE a full train.
Checks: processor accepts nested multi-audio, labels mask only the target, forward gives a
finite loss. Also runs a base-model generate on one multi-chunk clip to confirm interleave
inference works.
"""

import sys

import torch
from data_interleave import ChunkInterleaveDataset, InterleaveCollator, build_prompt
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

MANIFEST = sys.argv[1] if len(sys.argv) > 1 else "/home/wispr_interleave/manifest.jsonl"
MODEL = "google/gemma-4-E2B-it"

proc = AutoProcessor.from_pretrained(MODEL)
ds = ChunkInterleaveDataset(MANIFEST, "train")
print(
    f"train clips={len(ds)}; chunk counts (first 10): {[len(ds[i]['chunks']) for i in range(min(10,len(ds)))]}"
)
# pick a 1-chunk and a multi-chunk example to stress the collator
idx_multi = next((i for i in range(len(ds)) if len(ds[i]["chunks"]) >= 2), 0)
idx_one = next((i for i in range(len(ds)) if len(ds[i]["chunks"]) == 1), 0)
feats = [ds[idx_one], ds[idx_multi]]
print(
    f"batch: 1-chunk='{feats[0]['clip']}' ({len(feats[0]['chunks'])}), "
    f"multi='{feats[1]['clip']}' ({len(feats[1]['chunks'])} chunks, app={feats[1]['app']})"
)

coll = InterleaveCollator(proc)
batch = coll(feats)
print("batch keys:", list(batch.keys()))
print(
    "input_ids",
    tuple(batch["input_ids"].shape),
    "| input_features",
    tuple(batch["input_features"].shape) if "input_features" in batch else None,
)
sup = (batch["labels"] != -100).sum(dim=1).tolist()
print("supervised tokens/example (should ≈ target lengths):", sup)
# decode the supervised span of example 0 to confirm it's the target, not the prompt
lab = batch["labels"][0]
sup_ids = batch["input_ids"][0][lab != -100]
print("supervised decode[0]:", proc.decode(sup_ids, skip_special_tokens=True)[:120])

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
    MODEL,
    quantization_config=bnb,
    dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="sdpa",
)
model.config.use_cache = False
batch = {k: v.to(model.device) for k, v in batch.items()}
with torch.no_grad():
    out = model(**batch)
print(
    "FORWARD loss:", float(out.loss), "(finite:", torch.isfinite(out.loss).item(), ")"
)

# interleave generate on the multi-chunk clip (base model)
model.config.use_cache = True
f = ds[idx_multi]
content = [{"type": "text", "text": build_prompt(f["app"])}]
content += [{"type": "audio", "audio": a} for a in f["chunks"]]
inp = proc.apply_chat_template(
    [{"role": "user", "content": content}],
    tokenize=True,
    return_dict=True,
    return_tensors="pt",
    add_generation_prompt=True,
    enable_thinking=False,
).to(model.device)
g = model.generate(**inp, max_new_tokens=200, do_sample=False, use_cache=True)
print(
    "GEN (base, interleave):",
    proc.decode(g[0][inp["input_ids"].shape[-1] :], skip_special_tokens=True)[:200],
)
print("TARGET:", f["target"][:200])
print("SMOKE OK")
