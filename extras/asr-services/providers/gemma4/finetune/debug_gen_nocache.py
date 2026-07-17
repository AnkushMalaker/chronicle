"""Test: does generating with use_cache=False make the v3 adapter reproduce targets?

The cached (use_cache=True) gemma4 multimodal forward gives wrong logits (loss 4.49
vs 0.10 uncached), so model.generate() — which uses the cache by default — fails.
Force use_cache=False during generation and compare.
"""

from difflib import SequenceMatcher

import torch
from data import DEFAULT_PROMPT, CosheSample7Dataset
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

proc = AutoProcessor.from_pretrained("google/gemma-4-E2B-it")
proc.tokenizer.padding_side = "left"
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
m = AutoModelForMultimodalLM.from_pretrained(
    "google/gemma-4-E2B-it",
    quantization_config=bnb,
    dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)
pm = PeftModel.from_pretrained(m, "/train/out/e2b-overfit-v3")
pm.eval()

ds = CosheSample7Dataset(
    "/data/coshe-eval/sample7", max_seconds=30.0, target_max_chars=540
)
print("===== generate(use_cache=False) =====", flush=True)
ratios = []
for item in ds:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DEFAULT_PROMPT},
                {"type": "audio", "audio": item["audio"]},
            ],
        }
    ]
    ptext = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inp = proc(text=[ptext], audio=[item["audio"]], return_tensors="pt").to(pm.device)
    inlen = inp["input_ids"].shape[-1]
    with torch.inference_mode():
        out = pm.generate(**inp, max_new_tokens=320, do_sample=False, use_cache=False)
    gen = proc.decode(out[0][inlen:], skip_special_tokens=True).strip()
    r = SequenceMatcher(None, gen, item["target"]).ratio()
    ratios.append(r)
    print(f"\n  {item['name']} sim={r:.3f}", flush=True)
    print(f"    TGT: {item['target'][:220]}", flush=True)
    print(f"    GEN: {gen[:220]}", flush=True)
print(f"\n  MEAN char-sim = {sum(ratios)/len(ratios):.3f}", flush=True)
