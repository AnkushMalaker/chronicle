"""P6 -- the design the measurements point to.

Every choice here is a result from the other five pipelines, not a preference:

* **Fixed windows for coverage, not signal regimes.** No cheap deterministic key
  segments this stream into activity-sized units (P2's signal mode cut an 11-hour
  day 409 times). Equal windows guarantee that no stretch of the day goes unread,
  which is the failure mode of both the model-driven probe (P3 missed whole
  afternoons, and did so differently on each run) and the single long prompt (P5
  lost the middle of the day).

* **Anchors offered, not hunted.** Each window carries its top typographically
  salient frames, which is the only ranker measured to surface decisive frames
  (result screens at ranks 7 and 9 of 2821, versus 1667-2627 for the text-length
  ranker Chronicle uses today).

* **Pixels on a fixed budget, spent where text is unsure.** The escalation loop
  from P2, with the requests coming from the model and the budget from the caller.

* **Two-pass promotion instead of one-shot importance.** P1 emitted 38 events for
  this day, most of them research upgrades completing inside a match. Rather than
  ask a model to judge importance while it is still reading pixels, the first pass
  records everything it can establish and a second pass decides what is an event
  worth keeping, what is a detail belonging to another event, and what is noise.
  The literature survey's warning against absolute LLM importance scores
  (uncalibrated, 70-80% self-consistent) is the reason this is a *relative*
  ranking within a day rather than a per-event score threshold.

* **An explicit attribution pass.** The traps in this archive are all attribution
  failures, and P1 got m2's outcome backwards by reading a surrender as the
  opponent's. Checking authorship as its own step, with the assertion roles in
  front of the model, is cheaper than hoping a general instruction covers it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from ..evidence import rank_candidates
from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text, frame_signals, timeline_digest
from ..spipe import Frame, open_archive
from ..visual import visual_signals
from .p2_segment_escalate import _fixed_windows, _signal_note

IDENTITY_NOTE = """
Known facts about the person whose screen this is. Treat these as established, and
use them to decide whose outcome an event reports:
{identities}
"""

PASS1 = """{rules}
{identity}

You are reading one window of a capture day, in order. This is pass one: establish
what happened here. A later pass will decide what is worth keeping, so record
anything you can support, and do not leave something out because it seems minor.

Window {index} of {total}: {start} to {end} UTC ({minutes:.1f} min, {n} frames).
Deterministic signals: {signals}

The signal layer ranked these frames as most likely to carry a state change. It
ranks by the size of the largest confidently-read text on screen, so it finds
screens that announce something -- and equally, screens with a big title that
announce nothing. It does not know what any of them mean:
{anchors}

Events established in earlier windows:
{prior}

On-screen text, as [frame_id HH:MM:SSZ] text (UTC):
{text}

Return JSON:
{{
  "events": [{schema}],
  "continues": [{{"prior_index": 0, "what_changed": "...", "new_outcome": "..."}}],
  "needs_evidence": [
    {{"question": "...", "frame_ids": [...], "why": "..."}}
  ]
}}

Ask for pixels through "needs_evidence" when an outcome, amount, name or state is
unresolved and one specific frame would settle it -- at most {max_asks} frames.
Guessing instead of asking is the more expensive mistake."""

ESCALATE = """{rules}

Pixels for the frames you asked about in the window {start} to {end} UTC.

Your questions:
{questions}

{frame_notes}

Your provisional events:
{provisional}

Revise against what you can now see. OCR of large stylised text is often wrong
where the image is plain, so prefer the image where they disagree. Return JSON:
{{"events": [{schema}], "corrections": ["..."]}}"""

ATTRIBUTION = """{rules}
{identity}

Check the authorship of these extracted events before they are stored. This
archive contains three specific hazards, and each has caused a real error:

1. An agent session later in the day wrote *about* earlier activity. Its output
   contains outcome words and even a results table. An event dated to that
   session claiming the earlier activity happened then is wrong; the event that
   happened then is "someone analysed it".
2. Accessibility text includes background tabs and unopened menus. A name
   appearing only in such frames proves nothing about what the user was doing.
3. An outcome shown on screen belongs to someone. "X surrendered" and "surrendered
   to X" are different events with opposite results, and a result screen usually
   reports the outcome for the person at the keyboard.

Events:
{events}

For each event, confirm or correct: who did it, whose outcome it is, and whether
its time comes from evidence that something happened rather than from evidence
that something was discussed. Return JSON:
{{"events": [{schema}], "corrections": ["what you changed and why"]}}"""

PROMOTE = """{rules}

These events were extracted from one capture day. Decide what the day's record
should actually contain.

{events}

Three separate decisions, and they are not the same question:

1. **Is it an event?** Merge entries that are one happening seen twice. Fold
   entries that are steps inside a larger activity into that activity's
   attributes -- an upgrade completing during a match is not an event, the match
   is. Drop entries that only report that an application was open.
2. **How durable is it?** `ledger_only` for things worth being able to look up but
   not worth a note; `daily_note` for the day's shape; `durable_note` for facts
   that stay true and change how someone would act later -- a preference, a
   decision, a resolved problem, a commitment, a recurring result.
3. **How sensitive is it?** Mark health, financial and private material, since
   that governs whether it may be retained at all.

Rank durability *relative to the other events of this day* rather than against an
absolute bar, and expect most events to be `ledger_only`.

Return JSON: {{"events": [{schema}], "dropped": [{{"title": "...", "why": "..."}}]}}"""


def run(
    start: str,
    end: str,
    model: str = "gpt-5.4-mini",
    effort: str = "low",
    judge_model: str = "gpt-5.4",
    window_minutes: float = 12.0,
    image_budget: int = 20,
    max_asks: int = 3,
    anchors_per_window: int = 6,
    known_identities: list | None = None,
    max_chars: int = 40_000,
    prior_window: int = 10,
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)
    frames = archive.frames(start, end)
    visuals = visual_signals(frames)
    sigs = {s.frame_id: s for s in frame_signals(frames)}

    identity = (
        IDENTITY_NOTE.format(identities="\n".join(f"- {i}" for i in known_identities))
        if known_identities
        else ""
    )

    windows = _fixed_windows(frames, minutes=window_minutes)
    events: list[Event] = []
    trace: list[dict] = []
    images_used = 0

    for index, chunk in enumerate(windows):
        prior = (
            "\n".join(
                f"[{i}] {e.event_type} {e.started_at} -> {e.ended_at or 'open'}: {e.title}"
                f" (outcome={e.outcome})"
                for i, e in list(enumerate(events))[-prior_window:]
            )
            or "(none yet)"
        )
        candidates = rank_candidates(archive, chunk, top_k=anchors_per_window)
        payload = llm.json_complete(
            PASS1.format(
                rules=EXTRACTION_RULES,
                identity=identity,
                index=index + 1,
                total=len(windows),
                start=chunk[0].timestamp.isoformat(),
                end=chunk[-1].timestamp.isoformat(),
                minutes=(chunk[-1].timestamp - chunk[0].timestamp).total_seconds() / 60,
                n=len(chunk),
                signals=_signal_note(chunk, visuals, sigs),
                anchors=json.dumps([c.summary() for c in candidates], indent=1),
                prior=prior,
                text=compact_text(chunk)[:max_chars],
                schema=EVENT_SCHEMA,
                max_asks=max_asks,
            )
        )
        if not isinstance(payload, dict):
            payload = {}
        found = [
            Event.from_model(e, f"window-{index}") for e in payload.get("events", [])
        ]
        asks = payload.get("needs_evidence") or []
        step = {
            "window": index,
            "range": [chunk[0].timestamp.isoformat(), chunk[-1].timestamp.isoformat()],
            "frames": len(chunk),
            "events_from_text": len(found),
            "asks": asks,
            "escalated": False,
        }

        if asks and found and images_used < image_budget:
            wanted: list[int] = []
            for ask in asks:
                for fid in ask.get("frame_ids") or []:
                    fid = int(fid) if str(fid).isdigit() else None
                    if fid and fid not in wanted and any(f.id == fid for f in chunk):
                        wanted.append(fid)
            for c in candidates:  # top up with the ranked anchors
                if len(wanted) >= max_asks:
                    break
                if c.frame_id not in wanted:
                    wanted.append(c.frame_id)
            wanted = wanted[: max(0, min(max_asks, image_budget - images_used))]

            images, notes = [], []
            for fid in wanted:
                try:
                    images.append(archive.frame_png(fid, 1280))
                except Exception as exc:
                    notes.append(f"[{fid}] pixels unavailable: {exc}")
                    continue
                frame = archive.frame(fid)
                notes.append(
                    f"[{fid} {frame.timestamp:%H:%M:%S}Z] ocr: {frame.text[:400]}"
                )
            if images:
                images_used += len(images)
                revised = llm.json_complete(
                    ESCALATE.format(
                        rules=EXTRACTION_RULES,
                        start=chunk[0].timestamp.isoformat(),
                        end=chunk[-1].timestamp.isoformat(),
                        questions=json.dumps(asks, indent=1),
                        frame_notes="\n".join(notes),
                        provisional=json.dumps([asdict(e) for e in found], indent=1)[
                            :8000
                        ],
                        schema=EVENT_SCHEMA,
                    ),
                    images=images,
                )
                if isinstance(revised, dict) and revised.get("events"):
                    found = [
                        Event.from_model(e, f"window-{index}-escalated")
                        for e in revised["events"]
                    ]
                    step["corrections"] = revised.get("corrections", [])
                step["escalated"] = True
                step["images"] = wanted

        events.extend(found)
        trace.append(step)

    raw_count = len(events)

    # ------------------------------------------------------- attribution pass
    if events:
        checked = llm.json_complete(
            ATTRIBUTION.format(
                rules=EXTRACTION_RULES,
                identity=identity,
                events=json.dumps([asdict(e) for e in events], indent=1, default=str)[
                    :140_000
                ],
                schema=EVENT_SCHEMA,
            ),
            model=judge_model,
            effort="medium",
        )
        if isinstance(checked, dict) and checked.get("events"):
            trace.append(
                {"stage": "attribution", "corrections": checked.get("corrections", [])}
            )
            events = [Event.from_model(e, "attributed") for e in checked["events"]]

    # --------------------------------------------------------- promotion pass
    if events:
        promoted = llm.json_complete(
            PROMOTE.format(
                rules=EXTRACTION_RULES,
                events=json.dumps([asdict(e) for e in events], indent=1, default=str)[
                    :140_000
                ],
                schema=EVENT_SCHEMA,
            ),
            model=judge_model,
            effort="medium",
        )
        if isinstance(promoted, dict) and promoted.get("events"):
            trace.append({"stage": "promotion", "dropped": promoted.get("dropped", [])})
            events = [Event.from_model(e, "promoted") for e in promoted["events"]]

    return RunRecord(
        pipeline="p6_recommended" + ("_identity" if known_identities else ""),
        params={
            "model": model,
            "judge_model": judge_model,
            "window_minutes": window_minutes,
            "image_budget": image_budget,
            "windows": len(windows),
            "anchors_per_window": anchors_per_window,
            "known_identities": known_identities or [],
        },
        events=events,
        usage=llm.usage.summary(),
        trace=trace,
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=len(frames),
        frames_viewed_as_image=images_used,
        notes=(
            f"{len(windows)} windows; {raw_count} events before attribution and promotion, "
            f"{len(events)} after."
        ),
    )
