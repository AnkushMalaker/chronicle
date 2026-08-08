#!/bin/sh
# Sequential gemma4 scene-description benchmark sweep.
#
# Runs each model in its own process so VRAM is released between stages -- the
# 12B needs ~24GB on a 40GB A100 and must not share with a resident E2B.
#
# Stage order is deliberate: the targeted sets (17 hand-verified frames) run
# first on both models, so if the box is lost we still have the accuracy
# comparison. The grid sweeps that follow are for throughput and for the
# capture-frequency question, and are the parts that are safe to lose.
set -u

PY=/home/audio-scene-venv/bin/python
cd /home/vlmbench/vlmbench || exit 1
export HF_HOME=/home/.cache/huggingface
export HF_HUB_OFFLINE=1
export TQDM_DISABLE=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
mkdir -p results

stage() {
  name=$1; shift
  echo "================ STAGE $name ================"
  start=$(date +%s)
  # shellcheck disable=SC2068
  $PY bench_gemma4.py $@ 2>&1 | grep -v "it/s\]"
  echo "---- stage $name finished in $(( $(date +%s) - start ))s ----"
}

# 1-2: accuracy on the hand-verified frames, both models, all four prompts,
#      with the need-more-info loop enabled.
stage e2b_targeted  --model google/gemma-4-E2B-it --frames frames/targeted \
  --prompts describe,structured,triage,event --loop --out results/e2b_targeted.jsonl

stage 12b_targeted  --model google/gemma-4-12B-it --frames frames/targeted \
  --prompts describe,structured,triage,event --loop --out results/12b_targeted.jsonl

# 3: capture-frequency question at 1 frame / 120s over the 11-hour day.
#    triage + structured only; describe/event add nothing to a coverage answer.
stage e2b_grid120   --model google/gemma-4-E2B-it --frames frames/grid120 \
  --prompts triage,structured --out results/e2b_grid120.jsonl

# 4: the same day at 1 frame / 600s on the big model, to see whether a slower
#    cadence with a better model beats a fast cadence with a small one.
stage 12b_grid600   --model google/gemma-4-12B-it --frames frames/grid600 \
  --prompts triage,structured,event --out results/12b_grid600.jsonl

# 5: E4B is cached too and sits between the two on size; cheap to include on
#    the targeted set only.
stage e4b_targeted  --model google/gemma-4-E4B-it --frames frames/targeted \
  --prompts structured,triage,event --out results/e4b_targeted.jsonl

echo "================ ALL STAGES DONE ================"
ls -la results/
