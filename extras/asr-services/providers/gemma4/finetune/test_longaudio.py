"""Does gemma4 accept a full ~55s clip in one forward (vs the 30s window)?

Prints token/feature counts and runs a forward at 30s and ~60s to see if it
errors or how VRAM scales. Decides whether full 1-min clips are usable for
training without chunking.
"""

import torch
from data import DEFAULT_PROMPT, _load_wav_16k_mono
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

WAV = "/data/coshe-eval/sample7/audio/audio_13.wav"

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
model.config.use_cache = False
model.eval()

for secs in [30.0, 60.0]:
    audio = _load_wav_16k_mono(WAV, secs)
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DEFAULT_PROMPT},
                {"type": "audio", "audio": audio},
            ],
        }
    ]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    batch = proc(text=[text], audio=[audio], return_tensors="pt").to(model.device)
    torch.cuda.reset_peak_memory_stats()
    try:
        with torch.inference_mode():
            out = model(**batch)
        peak = torch.cuda.max_memory_allocated() / 1e9
        print(
            f"audio={secs:.0f}s  samples={len(audio)}  "
            f"input_ids={batch['input_ids'].shape[-1]}  "
            f"audio_frames={batch['input_features'].shape[1]}  "
            f"forward OK  logits={tuple(out.logits.shape)}  peakVRAM={peak:.1f}GB",
            flush=True,
        )
    except Exception as e:
        print(f"audio={secs:.0f}s  FAILED: {type(e).__name__}: {e}", flush=True)
