# Gemma 4 audio QLoRA fine-tuning

QLoRA (4-bit NF4) LoRA fine-tuning for `google/gemma-4-E2B-it` / `E4B-it` on an
audio→text ASR task. Built to validate the training+inference loop and the
single-4090 hardware setup by **overfitting** the 7-clip CoSHE-Eval `sample7`
subset (Hinglish, ~55s clips truncated to the model's 30s audio window).

## Approach (grounded in the official recipes)

Mirrors HuggingFace's official `fine_tune_gemma3n_on_audio.ipynb` data pipeline,
adapted to the **gemma4** family (this repo's model is `Gemma4ForConditionalGeneration`,
loaded via `AutoModelForMultimodalLM`, transformers >= 5.5):

- **Chat-message training examples**: `{user: [audio, prompt]}` + `{assistant: target}`,
  rendered with `apply_chat_template(tokenize=False)` then `processor(text=, audio=, padding=True)`.
- **Loss only on the transcription**: labels = `input_ids` with the prompt prefix
  (user turn incl. expanded audio soft tokens + `<|turn>model\n`), pad, and all
  multimodal special tokens masked to `-100`. (The official notebook leaves the
  prompt unmasked; we mask it — the cleaner ASR objective the research flagged.)
- **QLoRA**: NF4 + double-quant + bf16 compute. LoRA (r=16, α=32) on the **text
  decoder only** (`language_model.*.{q,k,v,o,gate,up,down}_proj`, regex-scoped so
  it doesn't hit the identically-named audio-tower linears). Audio/vision towers
  and multimodal embedders are **frozen and kept in bf16** (not quantized).

### Two gemma4-specific gotchas (vs. the gemma3n recipes)

1. **Don't 4-bit-quantize the audio/vision towers.** Their `Gemma4ClippableLinear`
   calls `torch.finfo(weight.dtype)` for gradient clipping, which throws on
   uint8-stored 4-bit weights. We pass `llm_int8_skip_modules=["model.audio_tower",
   "model.vision_tower", "model.embed_audio", "model.embed_vision", "lm_head"]`.
   Note the `model.` prefix is required — transformers' `should_convert_module`
   matches skip patterns with `re.match` (anchored at start), so bare `audio_tower`
   never matches `model.audio_tower....`.
2. **Don't use `prepare_model_for_kbit_training`.** It upcasts every non-4bit
   param to fp32, making the text embeddings fp32 while the (bf16) audio tower
   stays bf16 → Gemma4's multimodal `masked_scatter` merge errors on the dtype
   clash. We do manual prep (freeze base + `gradient_checkpointing_enable(use_reentrant=False)`
   + `enable_input_require_grads`), keeping everything bf16.

## Environment

Runs inside the `chronicle-asr-gemma4` image (transformers 5.5, torch cu126) with
`peft` + `bitsandbytes` added. A persistent container is the fastest iteration loop:

```bash
cd extras/asr-services
docker run -d --name gemma4-train --gpus all \
  -v "$PWD/model_cache:/models" \
  -v "$PWD/providers/gemma4/finetune:/train" \
  -v "$PWD/../ml-experiments/data/coshe-eval:/data/coshe-eval" \
  -e HF_HOME=/models \
  chronicle-asr-gemma4:latest sleep infinity
docker exec gemma4-train uv pip install --python /app/.venv/bin/python \
  "peft>=0.17.0" "bitsandbytes>=0.46.1"
```

## Run

```bash
# train (E2B overfit)
docker exec gemma4-train bash -c 'export PATH=/app/.venv/bin:$PATH; cd /train && \
  python train.py --model google/gemma-4-E2B-it --output_dir /train/out/e2b-overfit \
    --epochs 60 --lr 2e-4'

# verify: transcribe the training clips with the adapter, compare to ground truth
docker exec gemma4-train bash -c 'export PATH=/app/.venv/bin:$PATH; cd /train && \
  python infer.py --model google/gemma-4-E2B-it --adapter /train/out/e2b-overfit'
```

Switch `--model google/gemma-4-E4B-it` for the larger model (also fits 24GB in 4-bit).

## Files

- `data.py` — dataset (manifest → 16k mono, ≤30s) + multimodal collator with label masking
- `train.py` — 4-bit load, LoRA, HF `Trainer` overfit loop
- `infer.py` — adapter inference + char-similarity vs. ground truth
- `introspect.py` — dumps processor keys / token ids / module names for a model

## Results (E2B overfit smoke test) — IT OVERFITS

The overfit works: with `r=16` attn+MLP LoRA on the text decoder, **5 of 7 clips
reproduce the ground-truth transcript exactly** (char-sim 1.000) and mean char-sim
is **0.869** (the two misses, 0.66 / 0.43, are perfect for the first ~150 chars
then drift in the back half — the part of the 540-char target that runs past the
audible 30s, i.e. correct behaviour). Final teacher-forced loss ~0.006–0.12.

### The bug that hid it: gemma4 produces wrong logits with the KV cache on

Until we found this, generation looked broken (char-sim ≈ base 0.07). Root cause:

**`use_cache=True` makes gemma4's multimodal forward produce wrong audio-conditioned
logits (transformers 5.5.0).** Measured on a fully-overfit adapter, same weights,
same input:

| forward | loss on a memorized clip |
|---------|--------------------------|
| `use_cache=False` (training path) | **0.10** |
| `use_cache=True`  (generation default) | **4.49** (≈ base 4.84) |

Training runs with `use_cache=False`, so loss collapsed correctly. But
`model.generate()` defaults to the cache, so every generation silently used the
broken path and reverted to near-base output. **Fix: generate with
`use_cache=False`** (`infer.py` sets `model.config.use_cache=False` and passes
`use_cache=False` to `generate`). Slower (no KV cache → full recompute per token),
but correct. The proper fix is upstream in the gemma4 modeling code.

This was NOT exposure bias and NOT a capacity problem — the earlier v1/v2 "fixes"
(full transcript, lm_head+embed at r64) were all measured through the broken cached
path and so looked like failures/degeneration.

## What this validated

- **Overfit demonstrated end-to-end**: load 4-bit → LoRA → train → save → reload →
  generate reproduces the training transcripts (mean char-sim 0.869, 5/7 exact).
- 4-bit QLoRA on a single 4090 works for gemma4 audio.
- VRAM: lean r16 ≈ 18–19 GB; r64 + lm_head/embed_tokens ≈ 23.7 GB (fits, barely).
  bf16 audio/vision towers stay resident; activations gradient-checkpointed over
  ~750 audio + text tokens.
- Speed: ~7 min / 60 epochs (r16). E4B (~5 GB in 4-bit) also fits with room.
- **Debugging note**: `debug_inprocess.py` (train+eval, no save/reload),
  `debug_reload_loss.py` (use_cache toggle), `debug_gen_nocache.py` (no-cache
  generation) are the scripts that localized the cache bug — keep for reference.

## For a real fine-tune (next step)

Need many ≤30s clips with transcripts aligned to the audible window (not full-clip
transcripts truncated by char count). Generation must use `use_cache=False` until
the upstream cache bug is fixed; budget for the slower decode.
