"""Instrument the gemma4 cache-mask logic to fix the use_cache prefill bug.

Runs a single full forward (use_cache=True) on a memorized sample7 clip with the
s7-full adapter and prints what create_causal_mask_mapping sees (cache type,
get_seq_length, is_initialized, chosen is_first_iteration) + the resulting loss.
Goal: find the right prefill test so loss drops 4.49 -> ~0.1 with the cache on.
"""

import torch
import transformers.models.gemma4.modeling_gemma4 as g4
from data import DEFAULT_PROMPT, CosheSample7Dataset, Gemma4AudioCollator
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

_orig = g4.create_causal_mask_mapping
_seen = {}


def _instrumented(
    config,
    inputs_embeds,
    attention_mask,
    past_key_values,
    position_ids,
    mm_token_type_ids=None,
    pixel_values=None,
    is_training=False,
    is_first_iteration=None,
    **kwargs,
):
    pkv = past_key_values
    info = {
        "pkv_type": type(pkv).__name__ if pkv is not None else None,
        "is_initialized": (
            getattr(pkv, "is_initialized", "n/a") if pkv is not None else None
        ),
        "get_seq_length": (pkv.get_seq_length() if pkv is not None else None),
        "mm_token_type_ids_none": mm_token_type_ids is None,
        "q_len": inputs_embeds.shape[1],
        "pixel_values_none": pixel_values is None,
    }
    if not _seen:
        _seen.update(info)
        print("MASK CALL INFO:", info, flush=True)
    return _orig(
        config,
        inputs_embeds,
        attention_mask,
        past_key_values,
        position_ids,
        mm_token_type_ids=mm_token_type_ids,
        pixel_values=pixel_values,
        is_training=is_training,
        is_first_iteration=is_first_iteration,
        **kwargs,
    )


g4.create_causal_mask_mapping = _instrumented

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
model = PeftModel.from_pretrained(model, "/train/out/s7-full")
model.eval()

ds = CosheSample7Dataset(
    "/data/coshe-eval/sample7", max_seconds=30.0, target_max_chars=0
)
col = Gemma4AudioCollator(proc)
b = col([ds[0]])
b = {k: v.to(model.device) for k, v in b.items()}

for uc in [False, True]:
    model.config.use_cache = uc
    _seen.clear()
    with torch.inference_mode():
        loss = float(model(**b, use_cache=uc).loss)
    print(f"use_cache={uc}: loss={loss:.4f}", flush=True)


# now try forcing is_first_iteration=True always (prefill-style) to confirm theory
def _force_true(
    config,
    inputs_embeds,
    attention_mask,
    past_key_values,
    position_ids,
    mm_token_type_ids=None,
    pixel_values=None,
    is_training=False,
    is_first_iteration=None,
    **kwargs,
):
    return _orig(
        config,
        inputs_embeds,
        attention_mask,
        past_key_values,
        position_ids,
        mm_token_type_ids=mm_token_type_ids,
        pixel_values=pixel_values,
        is_training=is_training,
        is_first_iteration=True,
        **kwargs,
    )


g4.create_causal_mask_mapping = _force_true
model.config.use_cache = True
with torch.inference_mode():
    loss = float(model(**b, use_cache=True).loss)
print(f"use_cache=True + force is_first_iteration=True: loss={loss:.4f}", flush=True)
