"""Introspect a Gemma 4 E*B model + processor for QLoRA fine-tuning.

Loads the model in 4-bit and dumps the facts we need to write the training
script correctly for THIS model (the gemma4 family), since published recipes
target gemma3n and key/module names can differ:

  1. Processor output keys for a (text + audio) example, with shapes/dtypes.
  2. Audio/image special token ids exposed on the tokenizer/config.
  3. Module-name families for LoRA targets: text-decoder projections, the
     audio projector, and embedding/lm_head modules.

Run inside the gemma4 image (transformers >= 5.5).
"""

import os
import wave
from collections import Counter

import numpy as np
import torch
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

MODEL_ID = os.getenv("ASR_MODEL", "google/gemma-4-E2B-it")
AUDIO = os.getenv("PROBE_AUDIO", "/data/coshe-eval/sample7/audio/audio_13.wav")
PROMPT = "Transcribe the following speech segment in its original language."


def load_wav_16k_mono(path: str, max_seconds: float = 30.0) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    # mono assumed (sample7 is mono). Resample to 16k via librosa.
    if sr != 16000:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    audio = audio[: int(16000 * max_seconds)]
    return audio


def main():
    print(f"=== MODEL: {MODEL_ID} ===", flush=True)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForMultimodalLM.from_pretrained(
        MODEL_ID,
        quantization_config=bnb,
        dtype=torch.bfloat16,
        device_map="auto",
    )

    tok = processor.tokenizer
    print("\n=== SPECIAL TOKEN IDS ===", flush=True)
    for attr in [
        "pad_token_id",
        "eos_token_id",
        "bos_token_id",
        "audio_token_id",
        "image_token_id",
        "boi_token_id",
        "eoi_token_id",
        "boa_token_id",
        "eoa_token_id",
    ]:
        print(f"  tokenizer.{attr} = {getattr(tok, attr, '<none>')}", flush=True)
    cfg = model.config
    for attr in [
        "audio_token_id",
        "image_token_id",
        "boi_token_id",
        "eoi_token_id",
        "boa_token_id",
        "eoa_token_id",
        "eoa_token_index",
    ]:
        print(f"  config.{attr} = {getattr(cfg, attr, '<none>')}", flush=True)

    print("\n=== PROCESSOR OUTPUT (text+audio, TRAINING form) ===", flush=True)
    audio = load_wav_16k_mono(AUDIO)
    print(
        f"  probe audio: {AUDIO}  samples={len(audio)} ({len(audio)/16000:.1f}s)",
        flush=True,
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "audio", "audio": audio},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Speaker 1: hello world"}],
        },
    ]
    # tokenize=False then call processor with text+audio (the HF recipe pattern)
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=False
    )
    print(f"  templated text (first 300 chars):\n    {text[:300]!r}", flush=True)
    batch = processor(text=[text], audio=[audio], return_tensors="pt", padding=True)
    for k, v in batch.items():
        if hasattr(v, "shape"):
            print(f"  key {k:24s} shape={tuple(v.shape)} dtype={v.dtype}", flush=True)
        else:
            print(f"  key {k:24s} type={type(v)}", flush=True)

    print("\n=== MODULE NAME FAMILIES (Linear leaf modules) ===", flush=True)
    fam = Counter()
    audio_like = set()
    embed_like = set()
    for name, mod in model.named_modules():
        leaf = name.split(".")[-1]
        cls = type(mod).__name__
        if "Linear" in cls or "lora" in cls.lower():
            fam[leaf] += 1
        low = name.lower()
        if any(t in low for t in ["audio", "speech", "conformer"]):
            # record short module suffixes from the audio tower / projector
            if "Linear" in cls or "Embedding" in cls or "proj" in low:
                audio_like.add(f"{leaf} ({cls})")
        if "embed" in low or leaf in ("lm_head",):
            embed_like.add(f"{name} ({cls})")
    print("  Linear leaf-name -> count:", flush=True)
    for leaf, c in fam.most_common():
        print(f"    {leaf:28s} x{c}", flush=True)
    print("\n  audio-tower / projector linear+embedding leaves (sample):", flush=True)
    for s in sorted(audio_like)[:40]:
        print(f"    {s}", flush=True)
    print("\n  embedding / lm_head modules (full names, sample):", flush=True)
    for s in sorted(embed_like)[:25]:
        print(f"    {s}", flush=True)

    print("\n=== top-level named children ===", flush=True)
    for name, _ in model.named_children():
        print(f"    {name}", flush=True)
    # one level deeper into the LM + audio tower roots
    for root in [
        "model",
        "language_model",
        "audio_tower",
        "audio_model",
        "multi_modal_projector",
    ]:
        sub = getattr(model, root, None)
        if sub is not None:
            kids = [n for n, _ in sub.named_children()]
            print(f"    {root}.children = {kids}", flush=True)

    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
