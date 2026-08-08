"""Score every completed run and print the comparison table.

uv run python summarize.py                # score all *-latest.json
uv run python summarize.py out/runs/a.json out/runs/b.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.evaluate import score_run  # noqa: E402

RUNS = Path(__file__).resolve().parent / "out" / "runs"


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]] or sorted(RUNS.glob("*-latest.json"))
    rows = []
    for path in paths:
        cached = Path(str(path).replace(".json", ".score.json"))
        if cached.exists():
            rows.append(json.loads(cached.read_text()))
            continue
        try:
            score = score_run(path)
        except Exception as exc:
            print(f"!! {path.name}: {exc}", file=sys.stderr)
            continue
        row = asdict(score)
        cached.write_text(json.dumps(row, indent=2))
        rows.append(row)

    rows.sort(key=lambda r: (-r["must_found"], -r["outcomes_correct"], r["cost_usd"]))

    head = (
        f"| {'pipeline':<22} | matches | non-game | outcomes | traps | not-events | "
        f"events | images | cost | wall |"
    )
    print(head)
    print("|" + "|".join("-" * len(c) for c in head.split("|")[1:-1]) + "|")
    for r in rows:
        print(
            f"| {r['pipeline']:<22} | {r['must_found']}/{r['must_total']}     | "
            f"{r['should_found']}/{r['should_total']}      | "
            f"{r['outcomes_correct']}/{r['outcomes_judged']}      | "
            f"{r['trap_violations']}     | {r['not_events']}          | "
            f"{r['events_reported']:>6} | {r['frames_viewed']:>6} | "
            f"${r['cost_usd']:.3f} | {r['wall_seconds']:.0f}s |"
        )

    print("\nper-pipeline detail:")
    for r in rows:
        print(f"\n### {r['pipeline']}")
        for m in r["detail"]["matches"]:
            if m.get("verdict") != "found" or m.get("outcome_correct") is False:
                print(
                    f"  {m.get('truth_key')}: {m.get('verdict')}"
                    f" outcome_ok={m.get('outcome_correct')}"
                    f" -- {str(m.get('reasoning'))[:150]}"
                )
        for e in r["detail"]["extras"]:
            if e.get("class") in ("trap_violation", "not_an_event"):
                print(
                    f"  [{e.get('class')}] {str(e.get('trap_key') or '')} "
                    f"{str(e.get('reasoning'))[:140]}"
                )


if __name__ == "__main__":
    main()
