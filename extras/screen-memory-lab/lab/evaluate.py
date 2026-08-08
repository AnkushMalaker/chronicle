"""Score a prototype run against the hand-verified ground truth.

Three things are measured, and they are kept apart on purpose:

* **Recall** -- did the run find the events that are known to have happened, and
  did it get their decisive attributes right. The ground truth is exhaustive for
  the four game matches and for the main non-game activities, so recall is a fair
  number.

* **Trap violations** -- did the run assert something the archive actively baits
  it into asserting and that is false. These are counted separately from
  precision because they are the errors that would corrupt a memory store.

* **Extra events** -- everything else it reported. The ground truth is *not* an
  exhaustive list of every event in the day, so an unmatched event is not
  automatically wrong. Each one is judged as a plausible event the ground truth
  simply does not list, or as something that is not an event, or as a violation.

The alignment and the judgements are made by a model because event titles are
free text, but every judgement is recorded with its reasoning so the score can be
audited rather than trusted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .groundtruth import ALL_EVENTS, TRAPS
from .llm import LLM
from .spipe import parse_ts

JUDGE_MODEL = "gpt-5.4"

ALIGN_PROMPT = """You are scoring an automated event-extraction run against hand-verified
ground truth for one day of screen capture.

GROUND TRUTH EVENTS (verified by reading the archive and looking at the frames):
{truth}

CLAIMS THAT ARE FALSE -- the archive baits extractors into these, and asserting
any of them is an error, however confidently it is phrased:
{traps}

THE RUN'S OUTPUT:
{predicted}

For every ground truth event, decide whether the run found it. A ground truth
event counts as found if some predicted event refers to the same real-world
happening, even if worded differently, typed differently or timed loosely. Two
predicted events that each cover half of one ground truth event count as `partial`.
One predicted event that lumps several ground truth events together is `merged`
for each of them.

Then judge every predicted event that matched nothing.

Return JSON:
{{
  "matches": [
    {{"truth_key": "m1",
      "verdict": "found | partial | merged | missed",
      "predicted_index": 0,
      "outcome_correct": true|false|null,
      "attributes": {{"map": "correct|wrong|missing", "opponent": "...", "duration": "...", "outcome": "..."}},
      "timing_error_minutes": 0,
      "wrong_claims": ["any statement in the matched event that the ground truth contradicts"],
      "reasoning": "one or two sentences"}}
  ],
  "extras": [
    {{"predicted_index": 3,
      "class": "plausible_event | not_an_event | trap_violation | duplicate",
      "trap_key": "which trap, if any",
      "reasoning": "..."}}
  ]
}}

Judge strictly on evidence, not on fluency. An event whose outcome is stated
backwards is `found` with `outcome_correct: false`, not `missed`. An event that
merely mentions an activity without establishing that it happened is
`not_an_event`."""


@dataclass
class Score:
    pipeline: str
    must_found: int
    must_total: int
    should_found: int
    should_total: int
    outcomes_correct: int
    outcomes_judged: int
    attribute_correct: int
    attribute_total: int
    trap_violations: int
    not_events: int
    plausible_extras: int
    duplicates: int
    merged: int
    partial: int
    events_reported: int
    cost_usd: float
    calls: int
    wall_seconds: float
    frames_viewed: int
    detail: dict

    def line(self) -> str:
        return (
            f"{self.pipeline:<22} must {self.must_found}/{self.must_total}  "
            f"should {self.should_found}/{self.should_total}  "
            f"outcome {self.outcomes_correct}/{self.outcomes_judged}  "
            f"attrs {self.attribute_correct}/{self.attribute_total}  "
            f"traps {self.trap_violations}  not-events {self.not_events}  "
            f"extra {self.plausible_extras}  "
            f"${self.cost_usd:.3f}  {self.calls} calls  {self.wall_seconds:.0f}s  "
            f"{self.frames_viewed} imgs"
        )


def _truth_payload() -> str:
    rows = []
    for e in ALL_EVENTS:
        rows.append(
            {
                "key": e.key,
                "tier": e.tier,
                "event_type": e.event_type,
                "title": e.title,
                "start_utc": e.start,
                "end_utc": e.end,
                "attributes": e.attributes,
                "note": e.notes,
            }
        )
    return json.dumps(rows, indent=1)


def score_run(run_path: str | Path, judge_model: str = JUDGE_MODEL) -> Score:
    data = json.loads(Path(run_path).read_text())
    predicted = data["events"]
    slim = [
        {
            "index": i,
            "event_type": e.get("event_type"),
            "title": e.get("title"),
            "started_at": e.get("started_at"),
            "ended_at": e.get("ended_at"),
            "outcome": e.get("outcome"),
            "attributes": e.get("attributes"),
            "confidence": e.get("confidence"),
            "status": e.get("status"),
            "assertions": [
                {"claim": a.get("claim"), "role": a.get("role")}
                for a in (e.get("assertions") or [])
                if isinstance(a, dict)
            ][:8],
        }
        for i, e in enumerate(predicted)
    ]

    llm = LLM(model=judge_model, effort="medium")
    verdict = llm.json_complete(
        ALIGN_PROMPT.format(
            truth=_truth_payload(),
            traps=json.dumps(TRAPS, indent=1),
            predicted=json.dumps(slim, indent=1)[:200_000],
        ),
        max_output_tokens=20_000,
    )
    matches = verdict.get("matches", []) if isinstance(verdict, dict) else []
    extras = verdict.get("extras", []) if isinstance(verdict, dict) else []

    tiers = {e.key: e.tier for e in ALL_EVENTS}
    must_total = sum(1 for t in tiers.values() if t == "must")
    should_total = sum(1 for t in tiers.values() if t == "should")

    must_found = should_found = 0
    outcomes_correct = outcomes_judged = 0
    attr_ok = attr_total = 0
    merged = partial = 0
    for m in matches:
        key = m.get("truth_key")
        tier = tiers.get(key)
        found = m.get("verdict") in ("found", "merged", "partial")
        if m.get("verdict") == "merged":
            merged += 1
        if m.get("verdict") == "partial":
            partial += 1
        if found and tier == "must":
            must_found += 1
        if found and tier == "should":
            should_found += 1
        if m.get("outcome_correct") is not None:
            outcomes_judged += 1
            outcomes_correct += 1 if m["outcome_correct"] else 0
        for _, state in (m.get("attributes") or {}).items():
            attr_total += 1
            attr_ok += 1 if state == "correct" else 0

    classes = [e.get("class") for e in extras]
    usage = data.get("usage", {})
    return Score(
        pipeline=data["pipeline"],
        must_found=must_found,
        must_total=must_total,
        should_found=should_found,
        should_total=should_total,
        outcomes_correct=outcomes_correct,
        outcomes_judged=outcomes_judged,
        attribute_correct=attr_ok,
        attribute_total=attr_total,
        trap_violations=classes.count("trap_violation"),
        not_events=classes.count("not_an_event"),
        plausible_extras=classes.count("plausible_event"),
        duplicates=classes.count("duplicate"),
        merged=merged,
        partial=partial,
        events_reported=len(predicted),
        cost_usd=usage.get("cost_usd", 0.0),
        calls=usage.get("calls", 0),
        wall_seconds=data.get("wall_seconds", 0.0),
        frames_viewed=data.get("frames_viewed_as_image", 0),
        detail={
            "matches": matches,
            "extras": extras,
            "judge_usage": llm.usage.summary(),
        },
    )


def timing_error(pred_start: str, truth_start: str) -> float | None:
    try:
        return (
            abs((parse_ts(pred_start) - parse_ts(truth_start)).total_seconds()) / 60.0
        )
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        s = score_run(path)
        print(s.line())
        out = Path(path).with_suffix(".score.json")
        out.write_text(json.dumps(asdict(s), indent=2))
        print("  detail ->", out)
