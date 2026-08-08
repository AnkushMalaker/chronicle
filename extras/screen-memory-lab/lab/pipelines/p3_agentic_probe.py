"""P3 -- the model drives. A digest, a set of retrieval tools, and a loop.

The model starts with a ~7k-token index of the whole day and nothing else. It then
decides what to look at: it can grep the archive's text, read the OCR of a frame
range, look at a frame's pixels, ask the signal layer for ranked anchors, and
finally record events.

This is the design doc's "evidence broker" with the model in charge of what to
request. It is the most open-ended of the prototypes -- nothing tells it how many
events there are, what kind they might be, or where to look -- and it is the one
whose cost depends on the day rather than on a fixed schedule.

The tools are deliberately the same primitives a collector could expose over the
wire, so a pipeline that works here is implementable with the collector holding
the archive and the backend holding the model.
"""

from __future__ import annotations

import json
import time

from ..evidence import rank_candidates
from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text, timeline_digest
from ..spipe import open_archive, parse_ts

TOOLS = [
    {
        "type": "function",
        "name": "read_text",
        "description": (
            "Deduplicated on-screen text for a frame id range or time range. The "
            "cheapest way to look closely at a stretch of the day."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_frame": {"type": "integer"},
                "to_frame": {"type": "integer"},
                "max_chars": {"type": "integer", "description": "default 12000"},
            },
            "required": ["from_frame", "to_frame"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_text",
        "description": (
            "Find frames whose captured text contains a substring, anywhere in the "
            "capture period. Case-insensitive. Returns frame id, time, context and a "
            "short preview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "default 40"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "view_frame",
        "description": (
            "Look at a frame's actual pixels. Use when text is ambiguous, garbled, or "
            "when you need to confirm what a screen really showed. Costs far more than "
            "read_text, so choose the frame deliberately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "integer"},
                "why": {
                    "type": "string",
                    "description": "what this image should settle",
                },
            },
            "required": ["frame_id", "why"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "rank_anchors",
        "description": (
            "Ask the deterministic signal layer which frames in a range most look like "
            "a state change: transient text, novelty against the recent past, a change "
            "between two stable screens. Domain-blind; it does not know what the change "
            "means."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_frame": {"type": "integer"},
                "to_frame": {"type": "integer"},
                "top_k": {"type": "integer", "description": "default 10"},
            },
            "required": ["from_frame", "to_frame"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "record_events",
        "description": (
            "Record the events you have established. Call this as you go, not only at "
            "the end. Calling it with an empty list ends the investigation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "items": {"type": "object"}},
                "done": {
                    "type": "boolean",
                    "description": "true if the day is fully covered",
                },
            },
            "required": ["events"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = """You investigate a personal screen-capture archive and produce a record of
what happened. You have retrieval tools; use them to check things rather than
inferring from the index alone.

{rules}

Work like an investigator with a budget:
- The index below is compressed. Treat a bucket with unusual signals -- high
  churn, many OCR frames, a context you cannot name -- as something to look into,
  not something to summarise from the index.
- Prefer read_text and search_text. Spend view_frame only where pixels decide
  something text cannot.
- Cover the whole period. When you have finished one stretch, move on to the next
  unexplained one.
- Record events with record_events as you establish them, and set done=true only
  when every part of the period is either explained or deliberately dismissed as
  routine.

Each event must use this shape:
{schema}"""

KICKOFF = """Capture period: {start} to {end} UTC. Local time is Asia/Kolkata (UTC+05:30).
{n} frames were captured.

Index of the period, one row per {bucket} minutes. `new_tokens` are words that had
not appeared earlier in the day, `mean_churn` is how much the text changed between
consecutive frames (1.0 = nothing in common), `chrome_frames` are frames whose text
is menu/browser boilerplate from the accessibility tree rather than what was on
screen.

{digest}

Investigate this day and record what happened. You have at most {budget} tool
calls."""


def _unexamined_buckets(
    digest: list[dict], examined: set, events: list, min_frames: int = 20
):
    """Digest buckets that were never read and that no recorded event covers."""
    spans = []
    for e in events:
        try:
            spans.append((parse_ts(e.started_at), parse_ts(e.ended_at or e.started_at)))
        except Exception:
            continue

    out = []
    for bucket in digest:
        if bucket["frames"] < min_frames:
            continue
        lo, hi = bucket["frame_range"]
        span_ids = set(range(lo, hi + 1))
        if len(span_ids & examined) / max(1, len(span_ids)) > 0.25:
            continue
        b_from, b_to = parse_ts(bucket["from"]), parse_ts(bucket["to"])
        if any(s <= b_to and e >= b_from for s, e in spans):
            continue
        out.append(bucket)
    return out


def run(
    start: str,
    end: str,
    model: str = "gpt-5.4-mini",
    effort: str = "medium",
    bucket_minutes: int = 5,
    tool_budget: int = 60,
    image_budget: int = 20,
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)
    frames = archive.frames(start, end)
    by_id = {f.id: f for f in frames}
    digest = timeline_digest(frames, bucket_minutes=bucket_minutes)

    events: list[Event] = []
    trace: list[dict] = []
    prior: list[dict] = []
    images_used = 0
    text_frames_read = 0
    pending_images: list = []
    done = False
    coverage_warned = False
    examined: set = set()

    prompt = KICKOFF.format(
        start=start,
        end=end,
        n=len(frames),
        bucket=bucket_minutes,
        digest=json.dumps(digest, indent=1),
        budget=tool_budget,
    )
    system = SYSTEM.format(rules=EXTRACTION_RULES, schema=EVENT_SCHEMA)

    for turn in range(tool_budget):
        result = llm.complete(
            prompt,
            system=system,
            tools=TOOLS,
            prior=prior,
            images=pending_images or None,
        )
        pending_images = []
        prior = prior + [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ]
        prior += result["raw_items"]

        calls = result["tool_calls"]
        if not calls:
            trace.append({"turn": turn, "no_tool_call": (result["text"] or "")[:400]})
            break

        outputs = []
        for call in calls:
            args = json.loads(call["arguments"] or "{}")
            name = call["name"]
            record: dict = {"turn": turn, "tool": name, "args": args}

            if name == "read_text":
                lo, hi = int(args["from_frame"]), int(args["to_frame"])
                chunk = [f for f in frames if lo <= f.id <= hi]
                text_frames_read += len(chunk)
                examined.update(f.id for f in chunk)
                limit = int(args.get("max_chars") or 12_000)
                body = compact_text(chunk)
                if not chunk:
                    out = "(no frames in that range)"
                elif len(body) > limit:
                    # Never truncate silently. The first version of this tool did,
                    # and a request for 697 frames came back cut off mid-day: the
                    # model then merged two matches and reported the wrong outcome
                    # for both, with no way to know it had been shown half the
                    # evidence. Report the cut and where it happened.
                    kept = body[:limit]
                    covered = kept.count("\n[")
                    last = kept.rfind("\n[")
                    edge = kept[last + 2 : last + 20] if last != -1 else "?"
                    out = (
                        f"TRUNCATED. You asked for frames {lo}-{hi} ({len(chunk)} frames, "
                        f"{len(body)} chars) but only {limit} chars fit. Below are roughly "
                        f"the first {covered} distinct screens, ending near frame {edge}. "
                        f"The rest of the range was NOT shown to you. Re-request the "
                        f"remainder in smaller ranges before drawing conclusions about it.\n\n"
                        + kept
                    )
                    record["truncated"] = True
                else:
                    out = body
                record["frames"] = len(chunk)

            elif name == "search_text":
                hits = archive.grep(
                    args["query"],
                    start=start,
                    end=end,
                    limit=int(args.get("limit") or 40),
                )
                out = json.dumps(
                    [
                        {
                            "frame_id": h.id,
                            "utc": h.timestamp.strftime("%H:%M:%SZ"),
                            "source": h.text_source,
                            "context": h.context,
                            "preview": h.text[:160],
                        }
                        for h in hits
                    ],
                    indent=1,
                )
                record["hits"] = len(hits)

            elif name == "view_frame":
                fid = int(args["frame_id"])
                if images_used >= image_budget:
                    out = "image budget exhausted; rely on text"
                else:
                    try:
                        path = archive.frame_png(fid, 1280)
                        pending_images.append(path)
                        images_used += 1
                        frame = by_id.get(fid) or archive.frame(fid)
                        out = (
                            f"frame {fid} at {frame.timestamp:%H:%M:%SZ} UTC, "
                            f"context={frame.context}, text_source={frame.text_source}. "
                            "Image attached to the next message."
                        )
                    except Exception as exc:
                        out = f"pixels unavailable for frame {fid}: {exc}"
                record["images_used"] = images_used

            elif name == "rank_anchors":
                lo, hi = int(args["from_frame"]), int(args["to_frame"])
                chunk = [f for f in frames if lo <= f.id <= hi]
                found = rank_candidates(
                    archive, chunk, top_k=int(args.get("top_k") or 10)
                )
                out = json.dumps([c.summary() for c in found], indent=1)
                record["anchors"] = len(found)

            elif name == "record_events":
                batch = args.get("events") or []
                events.extend(Event.from_model(e, f"turn-{turn}") for e in batch)
                out = f"recorded {len(batch)} events; {len(events)} total"
                record["recorded"] = len(batch)

                # A coverage guard, not a nag. The model may legitimately dismiss a
                # stretch as routine, but it should not be able to finish while
                # busy stretches of the day were never looked at, which is how the
                # first version of this pipeline reported 4 events for 11 hours.
                if bool(args.get("done")):
                    unexamined = _unexamined_buckets(digest, examined, events)
                    if unexamined and not coverage_warned:
                        coverage_warned = True
                        out += (
                            "\n\nNot finished yet. These busy stretches were never read "
                            "and no recorded event covers them:\n"
                            + "\n".join(
                                f"  {b['from'][11:16]}-{b['to'][11:16]} frames "
                                f"{b['frame_range']}, {b['frames']} frames, "
                                f"new text: {' '.join(b['new_tokens'][:8])}"
                                for b in unexamined[:12]
                            )
                            + "\nLook at them, then call record_events(done=true) again. "
                            "If a stretch really is routine, say so in a one-line event "
                            "with status 'rejected' rather than leaving it unexplained."
                        )
                    else:
                        done = True
                record["done"] = done

            else:
                out = f"unknown tool {name}"

            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": out[:60_000],
                }
            )
            trace.append(record)

        prior += outputs
        prompt = (
            "Continue. Attached images correspond to your view_frame calls."
            if pending_images
            else "Continue."
        )
        if done:
            break

    return RunRecord(
        pipeline="p3_agentic_probe",
        params={
            "model": model,
            "effort": effort,
            "bucket_minutes": bucket_minutes,
            "tool_budget": tool_budget,
            "image_budget": image_budget,
        },
        events=events,
        usage=llm.usage.summary(),
        trace=trace,
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=text_frames_read,
        frames_viewed_as_image=images_used,
        notes=f"Model-driven retrieval. {len(trace)} tool calls, done={done}.",
    )
