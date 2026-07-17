# Qwen3-ASR LoRA fine-tuning on CoSHE (Hinglish)

LoRA fine-tuning of `Qwen/Qwen3-ASR-0.6B` (AuT audio encoder + Qwen3 text decoder, built on
Qwen3-Omni) on the **CoSHE-Eval** Hinglish dataset (1985 clips, mixed Devanagari+Latin
code-switched transcripts). Mirrors the gemma4 finetune dir layout but uses Qwen's official SFT
pipeline + a gemma4-style anneal/early-stop loop.

## Result — overfit goal achieved

**Full-CoSHE corpus WER = 1.18%** (1355/1985 clips reproduced exactly) — overfit to <2% on all
1985 clips. The sample7 smoke test reproduces 7/7 clips exactly (0.00% WER).

Qwen3-ASR is multimodal (audio→text) and natively produces **mixed Devanagari+Latin Hinglish**
(e.g. `complain कर लो technology evolve…`), with intra-sentence code-switching — confirmed
empirically on CoSHE.

## Files

- `coshe_infer.py` — transcribe CoSHE parquet shards with any Qwen3-ASR model → mlexp-schema JSONL
  (used for the base-model + srota baselines). **Use `--max_new_tokens 2048`**: the 512/1024
  default truncates CoSHE's long transcripts and inflates WER (the deletion-heavy artifact seen on srota).
- `make_manifest.py` / `make_manifest_full.py` — build `train.jsonl` (sample7) / `train_full.jsonl`
  (all 1985) in the official SFT schema: `{"audio": "/abs.wav", "text": "language None<asr_text>" + transcript}`.
- `introspect.py` — dump module names; LoRA targets the Qwen3 decoder `thinker.model.layers.*`
  (q/k/v/o/gate/up/down), keeping off the frozen AuT `thinker.audio_tower`.
- `qwen3_asr_sft.py` — vendored official trainer (`QwenLM/Qwen3-ASR/finetuning`); reused for its
  data/collator/`patch_outer_forward` (model.forward → `thinker.forward`) + label masking.
- `train_qwen3_until_wer.py` — LoRA training with gemma4-style anneal + loss-gated WER<target
  early-stop (subset gate → full-1985 confirm). Reuses the sft collator.
- `verify.py` — load base + adapter, transcribe clips, report char-sim / exact / WER.

## Recipe (what worked, and the gotchas)

Runs on the Jarvis Labs A100 (torch 2.11/cu130; `pip install qwen-asr datasets jiwer peft`; note
qwen-asr pins transformers 4.57). Single GPU.

1. **Smoke test (sample7, 7 clips):** `train_qwen3_until_wer.py --lora_r 16 --lr 2e-4 --epochs 50`
   → loss → 0.0001, **7/7 exact** once eval uses `--eval_max_new_tokens 2048` (512 truncates → false
   12.6% WER).
2. **Full CoSHE (1985):**
   - **Capacity matters.** Decoder-only LoRA (even r256) **plateaus at loss ~3.0** — it can't
     memorize 1985 long transcripts with a frozen output layer. `--include_head` (LoRA also on
     `lm_head` + `embed_tokens`, 239.8M params / 23.5%) breaks the wall → loss descends to ~0.08.
     (`Qwen3ASRForConditionalGeneration` doesn't expose `get_input_embeddings`; the script patches
     it to delegate to `thinker.model.embed_tokens` so PEFT can adapt the embeddings.)
   - **Warm then anneal (the gemma4 lesson).** Constant `lr 2e-4` drives loss to ~0.08 but WER
     **plateaus/bounces 11–22%** (memorizes some clips, too hot to settle the rest). Restart from
     the plateaued checkpoint with a **fresh optimizer at `lr 3e-5`** (`--init_adapter <ckpt>`) →
     loss collapses and **WER → 1.18% in a single anneal epoch.**
   - **Eval OOM.** The in-training WER eval (`wrapper.transcribe`) competes with training memory;
     use a small `--eval_chunk` (4–16) + `gc.collect()/empty_cache()` before eval, and
     `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.

## Run

```bash
# build full manifest (decodes 1985 clips → WAVs)
python3 make_manifest_full.py

# warm phase
python3 train_qwen3_until_wer.py --model Qwen/Qwen3-ASR-0.6B \
  --train_file train_full.jsonl --output_dir out/coshe-full-head \
  --lr 2e-4 --batch_size 2 --lora_r 256 --lora_alpha 512 --include_head \
  --eval_every 2 --eval_subset 200 --eval_chunk 16 --wer_target 0.02 --eval_max_new_tokens 2048

# anneal from the plateaued checkpoint (fresh optimizer, lower LR) until WER < 2%
python3 train_qwen3_until_wer.py --model Qwen/Qwen3-ASR-0.6B \
  --train_file train_full.jsonl --output_dir out/coshe-anneal \
  --init_adapter out/coshe-full-head/checkpoint-<latest> \
  --lr 3e-5 --batch_size 2 --eval_every 1 --eval_subset 200 --eval_chunk 16 --wer_target 0.02

# verify
python3 verify.py --adapter out/coshe-anneal --train_file train_full.jsonl --max_new_tokens 2048
```

## CoSHE WER comparison (IndicXlit-romanized, via mlexp `score_coshe`, 500 common clips)

| model | corpus WER | note |
|-------|-----------|------|
| Qwen3-ASR-0.6B + CoSHE LoRA (this) | **1.18%** | overfit goal (training-callback jiwer, full 1985) |
| Qwen3-ASR-0.6B (base, @2048 tok) | **25.16%** | base 0.6B — competitive with the 4B gemma4 |
| gemma4-E4B (base) | 26.23% | 4B base model |
| srota = qwen3-asr-0.6b-hinglish (out-of-domain FT) | 48.85% | trained on HiACC+OpenSLR, not CoSHE |

Findings:
- **The 1.18% is a memorization/overfit result** on the training set — it shows the model + LoRA
  recipe can fully fit CoSHE, not a generalization number.
- **Base Qwen3-ASR-0.6B (25.2%) ≈ gemma4-E4B (26.2%)** on CoSHE despite being ~7× smaller — a strong
  base model. (It has occasional greedy repetition-collapse: per-sample WER max 621%, so corpus WER
  has a heavy tail; a repetition guard would help.)
- **srota (the community Hinglish finetune) is *worse* than base (48.8% vs 25.2%)** on CoSHE — the
  out-of-domain SFT on HiACC/OpenSLR hurt generalization to this distribution (plus repetition-collapse
  and, in the original 1024-token run, truncation). A cautionary data point: a same-language finetune
  isn't automatically better on a different corpus.
