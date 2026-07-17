import torch
from data import CosheSample7Dataset, Gemma4AudioCollator
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

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
m = AutoModelForMultimodalLM.from_pretrained(
    "google/gemma-4-E2B-it",
    quantization_config=bnb,
    dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)
pm = PeftModel.from_pretrained(m, "/train/out/e2b-overfit-v3")
pm.eval()

bnorm = sum(float(p.float().norm()) for n, p in pm.named_parameters() if "lora_B" in n)
print("reloaded lora_B sum norm:", round(bnorm, 3), flush=True)

ds = CosheSample7Dataset(
    "/data/coshe-eval/sample7", max_seconds=30.0, target_max_chars=540
)
col = Gemma4AudioCollator(proc)

b0 = col([ds[0]])
b0 = {k: v.to(pm.device) for k, v in b0.items()}
for uc in [True, False]:
    pm.config.use_cache = uc
    if hasattr(pm, "base_model"):
        pm.base_model.config.use_cache = uc
    with torch.inference_mode():
        loss = float(pm(**b0).loss)
    print(f"  use_cache={uc}: reloaded-adapter loss = {loss:.4f}", flush=True)

with torch.inference_mode(), pm.disable_adapter():
    b = col([ds[0]])
    b = {k: v.to(pm.device) for k, v in b.items()}
    print("  base (adapter disabled) loss =", round(float(pm(**b).loss), 4), flush=True)
