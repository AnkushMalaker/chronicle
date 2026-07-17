"""Print a compact training status for a split-LoRA run: the val-loss series (from the
latest checkpoint's trainer_state.json), best eval_loss, current step, and any terminal
marker in the run's .log. Used by the progress monitor (the live stdout log is a single
\\r-joined line, so the structured state file is the reliable source)."""

import glob
import json
import sys

out = sys.argv[1].rstrip("/")
cks = sorted(glob.glob(out + "/checkpoint-*"), key=lambda p: int(p.split("-")[-1]))
evals, best, step = [], None, None
if cks:
    s = json.load(open(cks[-1] + "/trainer_state.json"))
    best = s.get("best_metric")
    step = s.get("global_step")
    for h in s["log_history"]:
        if "eval_loss" in h:
            evals.append((round(h["epoch"], 2), round(h["eval_loss"], 4)))
term = ""
try:
    raw = open(out + ".log").read()
    for m in [
        "DONE ",
        "Traceback",
        "CUDA out of memory",
        "RuntimeError",
        "OutOfMemory",
        "Killed",
    ]:
        if m in raw:
            term = m.strip()
            break
except Exception:
    pass
print(f"evals={len(evals)} step={step} best={best} series={evals[-6:]} TERM={term}")
