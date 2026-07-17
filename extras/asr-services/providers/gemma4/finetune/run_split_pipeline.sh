#!/bin/bash
# Drives the full split-LoRA generalization experiment on the A100, sequentially
# (single GPU). Each stage logs to its own file and appends a marker to a master
# progress log; a failed stage does not abort the rest. Window=30s "first-30s
# transcription" task throughout (proportional target truncation).
cd /home/gemma4ft || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
PROG=/home/gemma4ft/out/pipeline_progress.log
echo "PIPELINE_START $(date -u +%H:%M:%S)" > "$PROG"

stage () {  # stage <name> <logfile> <cmd...>
  local name="$1" log="$2"; shift 2
  echo "STAGE_START $name $(date -u +%H:%M:%S)" >> "$PROG"
  if "$@" > "$log" 2>&1; then echo "STAGE_OK $name $(date -u +%H:%M:%S)" >> "$PROG"
  else echo "STAGE_FAILED $name rc=$? $(date -u +%H:%M:%S)" >> "$PROG"; fi
}

COMMON="--lr 1e-5 --lora_r 16 --lora_alpha 32 --batch_size 2 --grad_accum 2 --patience 4 --epochs 40 --window_seconds 30"
EVAL_COMMON="--limit 700 --batch_size 8 --max_new_tokens 448 --window_seconds 30"

# 1) decoder-only r16
stage train_decoder out/split_decoder_r16.log \
  python3 train_lora_split.py --output_dir out/split_decoder_r16 $COMMON

# 2) include-head r16
stage train_head out/split_head_r16.log \
  python3 train_lora_split.py --output_dir out/split_head_r16 --include_head $COMMON

# 3) base eval (no adapter) — the controlled baseline
stage eval_base out/eval_base.log \
  python3 eval_test_split.py --out out/base_test.jsonl $EVAL_COMMON

# 4) ft decoder eval (guard: only if adapter saved)
if [ -f out/split_decoder_r16/adapter_model.safetensors ]; then
  stage eval_ft_decoder out/eval_ft_decoder.log \
    python3 eval_test_split.py --adapter out/split_decoder_r16 --out out/ft_decoder_test.jsonl $EVAL_COMMON
else
  echo "SKIP eval_ft_decoder (no adapter)" >> "$PROG"
fi

# 5) ft head eval (guard)
if [ -f out/split_head_r16/adapter_model.safetensors ]; then
  stage eval_ft_head out/eval_ft_head.log \
    python3 eval_test_split.py --adapter out/split_head_r16 --out out/ft_head_test.jsonl $EVAL_COMMON
else
  echo "SKIP eval_ft_head (no adapter)" >> "$PROG"
fi

echo "PIPELINE_DONE $(date -u +%H:%M:%S)" >> "$PROG"
