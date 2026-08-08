"""Run one or more extraction prototypes over a capture period and score them.

    uv run python run_lab.py --pipelines p1,p2,p3,p4,p5
    uv run python run_lab.py --pipelines p3 --model gpt-5.4 --tag strong
    uv run python run_lab.py --score-only

Runs are written to out/runs/ and scored against lab/groundtruth.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.evaluate import score_run  # noqa: E402
from lab.groundtruth import DAY_END, DAY_START  # noqa: E402
from lab.pipelines import (  # noqa: E402
    p1_fixed_windows,
    p2_segment_escalate,
    p3_agentic_probe,
    p4_anchor_induction,
    p5_long_context,
    p6_recommended,
)

PIPELINES = {
    "p1": p1_fixed_windows,
    "p2": p2_segment_escalate,
    "p3": p3_agentic_probe,
    "p4": p4_anchor_induction,
    "p5": p5_long_context,
    "p6": p6_recommended,
}

# The archive holds three days. Archetype induction is allowed to look at all of
# it, because recurrence across days is the signal it depends on; extraction is
# always scored on the single ground-truthed day.
INDUCTION_PERIOD = ("2026-07-22T00:00:00+00:00", "2026-07-25T01:00:00+00:00")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipelines", default="p1,p2,p3,p4,p5")
    ap.add_argument("--start", default=DAY_START)
    ap.add_argument("--end", default=DAY_END)
    ap.add_argument(
        "--model", default=None, help="override the pipeline's default model"
    )
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--judge-model",
        default=None,
        help="p6 only: model for the attribution and promotion passes",
    )
    ap.add_argument(
        "--identities",
        default=None,
        help="p6 only: semicolon-separated known facts about who the user is",
    )
    ap.add_argument(
        "--regime-mode",
        default=None,
        choices=["fixed", "signal"],
        help="p2 only: equal windows, or stretches derived from signals",
    )
    ap.add_argument(
        "--score-only", action="store_true", help="rescore existing -latest runs"
    )
    ap.add_argument("--no-score", action="store_true")
    opts = ap.parse_args()

    names = [n.strip() for n in opts.pipelines.split(",") if n.strip()]
    runs_dir = Path(__file__).resolve().parent / "out" / "runs"
    produced: list[Path] = []

    if opts.score_only:
        produced = [
            runs_dir / f"{PIPELINES[n].__name__.split('.')[-1]}-latest.json"
            for n in names
        ]
    else:
        for name in names:
            module = PIPELINES[name]
            kwargs: dict = {}
            if opts.model:
                kwargs["model"] = opts.model
            if opts.judge_model and name == "p6":
                kwargs["judge_model"] = opts.judge_model
            if name == "p4":
                kwargs["induce_from"] = INDUCTION_PERIOD
            if name == "p2" and opts.regime_mode:
                kwargs["regime_mode"] = opts.regime_mode
            if name == "p6" and opts.identities:
                kwargs["known_identities"] = [
                    s.strip() for s in opts.identities.split(";") if s.strip()
                ]
            print(f"\n=== {name}: {module.__name__} ===", flush=True)
            try:
                record = module.run(opts.start, opts.end, **kwargs)
            except Exception:
                print(f"!! {name} failed:\n{traceback.format_exc()}", flush=True)
                continue
            path = record.save(opts.tag)
            produced.append(path)
            print(record.brief(), flush=True)
            print("saved:", path, flush=True)

    if opts.no_score:
        return

    print("\n=== scores ===", flush=True)
    rows = []
    for path in produced:
        if not Path(path).exists():
            print(f"(missing {path})", flush=True)
            continue
        try:
            score = score_run(path)
        except Exception:
            print(f"!! scoring {path} failed:\n{traceback.format_exc()}", flush=True)
            continue
        print(score.line(), flush=True)
        Path(str(path).replace(".json", ".score.json")).write_text(
            json.dumps(asdict(score), indent=2)
        )
        rows.append(asdict(score))

    if rows:
        summary = runs_dir / f"summary{('-' + opts.tag) if opts.tag else ''}.json"
        summary.write_text(json.dumps(rows, indent=2))
        print("summary:", summary, flush=True)


if __name__ == "__main__":
    main()
