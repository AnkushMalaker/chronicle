#!/bin/sh
# Stage 2 of the gemma4 sweep.
#
# Two things the first sweep could not do:
#
# * The 12B checkpoint is `model_type: gemma4_unified` and needs transformers
#   >=5.10.dev, which the audio-scene venv (5.7.0) does not have. It runs here
#   against /home/tf-main-venv (5.15.0.dev0).
# * Two new prompts, `outcome` and `concluded`, added because E2B read
#   "Victory"/"Defeat" as salient text and still answered
#   is_state_announcement=false. They ask for the result directly instead of
#   asking the model to classify the screen type.
set -u

cd /home/vlmbench/vlmbench || exit 1
export HF_HOME=/home/.cache/huggingface
export HF_HUB_OFFLINE=1
export TQDM_DISABLE=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
mkdir -p results

NEW=/home/tf-main-venv/bin/python
OLD=/home/audio-scene-venv/bin/python

stage() {
  name=$1; py=$2; shift 2
  echo "================ STAGE $name ================"
  start=$(date +%s)
  # shellcheck disable=SC2068
  $py bench_gemma4.py $@
  echo "---- stage $name exit=$? in $(( $(date +%s) - start ))s ----"
}

# The reframed prompts on the small model first -- if framing is the whole
# story, this is where it shows up cheapest.
stage e2b_reframed $OLD --model google/gemma-4-E2B-it --frames frames/targeted \
  --prompts outcome,concluded --out results/e2b_reframed.jsonl

# The 12B on everything, including the original prompts, so it is comparable to
# the E2B numbers already collected.
stage 12b_targeted $NEW --model google/gemma-4-12B-it --frames frames/targeted \
  --prompts structured,triage,event,outcome,concluded --loop \
  --out results/12b_targeted.jsonl

# E4B sits between them and is already cached; same reframed prompts.
stage e4b_reframed $OLD --model google/gemma-4-E4B-it --frames frames/targeted \
  --prompts structured,outcome,concluded --out results/e4b_reframed.jsonl

# The 12B over the coarse grid, to price a whole day at 1 frame / 600s on the
# model that can actually judge.
stage 12b_grid600 $NEW --model google/gemma-4-12B-it --frames frames/grid600 \
  --prompts triage,outcome --out results/12b_grid600.jsonl

# Re-run last, in full. The first sweep's e2b_grid120 was stopped partway to
# free the GPU for the stages above, and the bench script truncates its output
# file on start, so a partial file cannot be resumed -- it runs from scratch
# here rather than being reported as if it were complete.
stage e2b_grid120 $OLD --model google/gemma-4-E2B-it --frames frames/grid120 \
  --prompts triage,outcome --out results/e2b_grid120.jsonl

echo "================ STAGE 2 DONE ================"
wc -l results/*.jsonl
