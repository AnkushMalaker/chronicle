"""P5 -- one giant prompt. The "just use a big context window" answer.

Deduplicated OCR for a whole capture day is about 170k tokens, which fits in
current long-context models. So the honest question is whether any structure is
needed at all, or whether you can hand the model the day and get the events back.

Cheap to test, and its failure modes are informative: what a single pass loses at
this length is the same thing it loses on a long audio recording -- the middle.
"""

from __future__ import annotations

import time

from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text
from ..spipe import open_archive

PROMPT = """{rules}

Below is the deduplicated on-screen text for one capture day, oldest first, as
[frame_id HH:MM:SSZ] text (UTC). Local time is Asia/Kolkata (UTC+05:30); the capture
period in UTC is {start} to {end}. Lines marked [accessibility] came from the
accessibility tree, not the pixels. "(+N near-identical frames)" means N frames
followed with essentially the same text.

{text}

Find every event in this day. Work through the day in order and do not stop
early; the middle of the day matters as much as the start. Return JSON:
{{"events": [{schema}]}}"""


def run(
    start: str,
    end: str,
    model: str = "gpt-5.4-mini",
    effort: str = "medium",
    max_chars: int = 900_000,
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)
    frames = archive.frames(start, end)
    text = compact_text(frames)
    truncated = len(text) > max_chars

    payload = llm.json_complete(
        PROMPT.format(
            rules=EXTRACTION_RULES,
            start=start,
            end=end,
            text=text[:max_chars],
            schema=EVENT_SCHEMA,
        ),
        max_output_tokens=32_000,
    )
    found = payload.get("events", []) if isinstance(payload, dict) else []

    return RunRecord(
        pipeline="p5_long_context",
        params={
            "model": model,
            "effort": effort,
            "chars": len(text),
            "truncated": truncated,
        },
        events=[Event.from_model(e, "single-pass") for e in found],
        usage=llm.usage.summary(),
        trace=[{"chars": len(text), "truncated": truncated, "frames": len(frames)}],
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=len(frames),
        notes="Single pass over the whole day's deduplicated text. No images, no iteration.",
    )
