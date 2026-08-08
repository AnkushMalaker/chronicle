"""The event contract every prototype emits, and the prompt language they share.

Keeping one schema and one set of extraction rules across prototypes is what
makes the comparison mean anything: the pipelines differ in *how they look at the
archive*, not in what they are asked to produce or what rules they follow.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parents[1] / "out" / "runs"

# The event vocabulary is deliberately open. The examples exist to show the shape
# of a type string, not to enumerate the allowed values -- an extractor that can
# only emit these has failed the point of the exercise.
EVENT_SCHEMA = """
{
  "event_type": "snake_case, open vocabulary, namespaced if useful. Invent one if
                 nothing fits. Examples of the SHAPE only: game_match_completed,
                 purchase_researched, software_fix_completed, media_watched,
                 document_submitted, person_committed_to_something.",
  "title":      "one sentence a person would recognise a week later",
  "started_at": "ISO 8601 UTC",
  "ended_at":   "ISO 8601 UTC",
  "outcome":    "how it ended, or null if it did not end / has no outcome",
  "attributes": {"free-form key/value facts specific to this kind of event": "..."},
  "assertions": [
    {"claim": "one fact",
     "role":  "user_action | user_statement | third_party | application_state | media_content | assistant_generated | uncertain",
     "evidence_frames": [123, 456],
     "confidence": 0.0}
  ],
  "confidence": 0.0,
  "status":     "confirmed | provisional | rejected",
  "evidence_frames": [123],
  "durability": "ledger_only | daily_note | durable_note",
  "sensitivity": "normal | private | health | financial"
}
""".strip()

# These rules are domain-blind on purpose. None of them mention games, shopping,
# or any other activity: they are about what screen text can and cannot prove.
EXTRACTION_RULES = """
You are reading a personal screen-capture archive to find things that HAPPENED.

What counts as an event: something with a beginning and an end that a person
would later want to recall or ask about -- an outcome, a decision, a completion,
a purchase, a state change, a commitment, a result. Not "an app was open", not
"text was on screen", not routine progress with no change of state.

Rules about evidence, which matter more than fluency:

1. Visible text is not automatically a fact about the user. Decide who authored
   what you are reading, and record it in `role`:
   - text the user typed or said           -> user_statement
   - text an application displayed         -> application_state
   - another person's message or speech    -> third_party
   - subtitles, lyrics, video content      -> media_content
   - output from an AI assistant, terminal
     agent, or chat model                  -> assistant_generated
   An assistant describing something is NOT evidence that it happened. A
   document or chat that discusses an earlier activity is evidence that the
   discussion happened, not that the activity happened then.

2. Frames marked [accessibility] may contain text that was never on screen:
   background browser tabs, unopened menus, offscreen elements. Never date an
   event from accessibility text alone, and never treat a name appearing there as
   proof the user was doing something.

3. A screen that stays up after something finished does not extend it. Date the
   end of an event from when it happened, not from the last frame that still
   showed the aftermath.

4. A list of available options is not a choice. Menus, catalogues, search results
   and pickers enumerate things the user did not necessarily do.

5. One continuous stretch of capture can contain several separate events, and one
   event can span several applications. Do not merge two events because the
   capture never changed application, and do not split one event because the user
   alt-tabbed away in the middle.

6. Cite frame ids for every assertion. If you cannot cite it, lower the
   confidence or leave it out. Prefer saying an attribute is unknown to guessing
   it.

7. If a later screen supersedes an earlier belief, the event's outcome is what
   the later evidence says.

8. Report the coarsest event a person would recognise. If several things you
   could report are steps within one activity that has one outcome, report the
   activity and put the steps in `attributes` if they matter. A person remembers
   "I lost that match"; they do not remember each upgrade that completed during
   it. Ask of each candidate event: would this person plausibly want to recall
   this, or be able to ask about it, a week later? If not, leave it out.

9. Frame times in the text you are given are UTC, marked with a trailing Z.
   Every timestamp you output must be UTC. The user's local timezone is
   Asia/Kolkata (UTC+05:30), which is relevant only for describing when
   something happened in words.
""".strip()


@dataclass
class Event:
    event_type: str
    title: str
    started_at: str
    ended_at: str | None = None
    outcome: str | None = None
    summary: str = ""
    attributes: dict = field(default_factory=dict)
    assertions: list = field(default_factory=list)
    confidence: float = 0.0
    status: str = "provisional"
    evidence_frames: list = field(default_factory=list)
    durability: str = "ledger_only"
    sensitivity: str = "normal"
    source_stage: str = ""

    @classmethod
    def from_model(cls, raw: dict, stage: str = "") -> "Event":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in raw.items() if k in known}
        clean.setdefault("event_type", raw.get("type", "unknown"))
        clean.setdefault("title", raw.get("summary", "(untitled)"))
        clean.setdefault("started_at", raw.get("start") or raw.get("from") or "")
        if not clean.get("ended_at"):
            clean["ended_at"] = raw.get("end") or raw.get("to")
        clean["source_stage"] = stage
        if isinstance(clean.get("evidence_frames"), (int, str)):
            clean["evidence_frames"] = [clean["evidence_frames"]]
        return cls(**clean)


@dataclass
class RunRecord:
    """Everything needed to compare and re-inspect one prototype run."""

    pipeline: str
    params: dict
    events: list
    usage: dict
    trace: list = field(default_factory=list)
    wall_seconds: float = 0.0
    frames_considered: int = 0
    frames_read_as_text: int = 0
    frames_viewed_as_image: int = 0
    notes: str = ""

    def save(self, tag: str = "") -> Path:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = RUNS_DIR / f"{self.pipeline}{('-' + tag) if tag else ''}-{stamp}.json"
        payload = asdict(self)
        payload["events"] = [
            e if isinstance(e, dict) else asdict(e) for e in self.events
        ]
        path.write_text(json.dumps(payload, indent=2, default=str))
        latest = RUNS_DIR / f"{self.pipeline}-latest.json"
        latest.write_text(json.dumps(payload, indent=2, default=str))
        return path

    def brief(self) -> str:
        lines = [
            f"{self.pipeline}: {len(self.events)} events, "
            f"{self.usage.get('calls', 0)} model calls, "
            f"${self.usage.get('cost_usd', 0):.4f}, {self.wall_seconds:.0f}s wall, "
            f"text-read {self.frames_read_as_text} frames, viewed {self.frames_viewed_as_image} images"
        ]
        for e in self.events:
            d = e if isinstance(e, dict) else asdict(e)
            lines.append(
                f"  - [{d.get('event_type')}] {d.get('started_at','')[11:19]}-"
                f"{(d.get('ended_at') or '')[11:19]} {d.get('title')} "
                f"(outcome={d.get('outcome')}, conf={d.get('confidence')})"
            )
        return "\n".join(lines)
