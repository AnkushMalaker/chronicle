"""P1 -- fixed time windows, text only. The control.

This is what a naive screen-memory pipeline does, and roughly what Chronicle's
current observation curator does: cut the day into equal windows, hand each
window's text to a model, ask what happened. No iteration, no images, no evidence
requests.

It exists to establish what the simplest thing gets right, so the more elaborate
pipelines have to earn their cost.
"""

from __future__ import annotations

import time
from datetime import timedelta

from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text
from ..spipe import open_archive

PROMPT = """{rules}

Capture window: {start} to {end} (UTC). Frames are shown as
[frame_id HH:MM:SSZ] text (UTC). Lines marked [accessibility] came from the
accessibility tree rather than the pixels.

{text}

List the events that happened in this window. Return JSON:
{{"events": [{schema}]}}
Return {{"events": []}} if nothing in this window is worth remembering."""


def run(
    start: str,
    end: str,
    window_minutes: int = 15,
    model: str = "gpt-5.4-mini",
    effort: str = "low",
    max_chars: int = 60_000,
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)
    frames = archive.frames(start, end)

    events: list[Event] = []
    trace: list[dict] = []
    text_frames = 0

    step = timedelta(minutes=window_minutes)
    cursor = frames[0].timestamp.replace(second=0, microsecond=0) if frames else None
    while cursor and cursor < frames[-1].timestamp:
        stop = cursor + step
        chunk = [f for f in frames if cursor <= f.timestamp < stop]
        cursor = stop
        if len(chunk) < 3:
            continue
        text = compact_text(chunk)[:max_chars]
        text_frames += len(chunk)
        payload = llm.json_complete(
            PROMPT.format(
                rules=EXTRACTION_RULES,
                start=chunk[0].timestamp.isoformat(),
                end=chunk[-1].timestamp.isoformat(),
                text=text,
                schema=EVENT_SCHEMA,
            )
        )
        found = payload.get("events", []) if isinstance(payload, dict) else []
        trace.append(
            {
                "window": [
                    chunk[0].timestamp.isoformat(),
                    chunk[-1].timestamp.isoformat(),
                ],
                "frames": len(chunk),
                "chars": len(text),
                "events": len(found),
            }
        )
        events.extend(Event.from_model(e, "window") for e in found)

    return RunRecord(
        pipeline="p1_fixed_windows",
        params={"window_minutes": window_minutes, "model": model, "effort": effort},
        events=events,
        usage=llm.usage.summary(),
        trace=trace,
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=text_frames,
        notes="No iteration, no images. Each window is judged with no knowledge of the others.",
    )
