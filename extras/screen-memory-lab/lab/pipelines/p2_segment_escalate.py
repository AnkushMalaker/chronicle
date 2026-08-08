"""P2 -- cheap regimes, then extract, then escalate only where it is unsure.

Three stages:

1. **Regimes.** Group frames into activity regimes using only deterministic
   signals: capture gaps, visual motion level, text churn, and application
   context when it exists. This does not try to find event boundaries -- the
   experiment in `docs/research/screen-memory/04-prototype-results.md` shows
   text-only boundary detection cannot find them -- it just cuts the day into
   stretches that are internally similar, deliberately over-segmenting.

2. **Extract.** One text-only pass per regime, carrying a short summary of the
   events found so far so a regime can continue or close an earlier one.

3. **Escalate.** Where the model reported an unresolved outcome, low confidence,
   or an unknown attribute, it gets a second look with actual pixels: the ranked
   anchor frames for that regime, plus the frames it explicitly asked about.

The escalation budget is fixed, so cost stays bounded no matter what the day
contained. This is the design in docs/multimodal-memory.md with the collector and
backend collapsed into one process.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict

from ..evidence import rank_candidates
from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text, frame_signals
from ..spipe import Frame, open_archive
from ..visual import visual_signals

EXTRACT_PROMPT = """{rules}

You are looking at one stretch of capture, part of a longer day.

Stretch {index}: {start} to {end} UTC ({minutes:.1f} minutes, {n} frames).
Deterministic signals for this stretch: {signals}

Events already established earlier today (do not repeat them; you may continue or
close one by referring to its index):
{prior}

On-screen text, as [frame_id HH:MM:SSZ] text (UTC):
{text}

Return JSON:
{{
  "events": [{schema}],
  "continues": [{{"prior_index": 0, "what_changed": "...", "new_outcome": "..."}}],
  "needs_evidence": [
    {{"question": "what you cannot answer from text alone",
      "frame_ids": [ids whose pixels would answer it],
      "why": "..."}}
  ]
}}

Use "needs_evidence" whenever an outcome, a result, an amount, a name or a state
is unresolved and a picture of a specific frame would settle it. Do not guess to
avoid asking. Ask for at most {max_asks} frames."""

ESCALATE_PROMPT = """{rules}

You asked for pixels to resolve open questions about this stretch of capture
({start} to {end} UTC).

Your open questions:
{questions}

Below are the requested frames plus the frames this stretch's signal layer ranked
as most likely to carry a state change. Each image is labelled with its frame id
and local time. The OCR text of these frames is included too, because OCR of
large stylised text is often wrong where the image is clear.

{frame_notes}

Your provisional events for this stretch:
{provisional}

Revise them against what you can now see. Correct outcomes, fill in attributes,
split an event that is really two, or reject one that is not supported. Return
JSON:
{{"events": [{schema}], "corrections": ["what the pixels changed about your text-only reading"]}}"""


def _regimes(
    frames: list[Frame],
    idle_gap_s: float = 240.0,
    target_minutes: float = 6.0,
    motion_split: float = 0.05,
    smooth: int = 0,
    persist_frames: int = 3,
    min_regime_frames: int = 2,
) -> list[list[Frame]]:
    """Cut the run into internally-similar stretches using signals only.

    ``smooth`` matters more than any other parameter here. Captured frames are 3+
    seconds apart, so raw per-frame motion flaps between "busy" and "calm" even
    inside one continuous activity: with ``smooth=0`` this produced 409 regimes
    for an 11-hour day, one every 100 seconds, which fragments events and makes
    the extraction pass cost scale with capture noise instead of with activity.
    Taking a rolling median of motion over ``smooth`` frames, and requiring a new
    regime to hold for ``persist_frames``, is what turns the signal into a
    description of the activity rather than of the sampling.
    """
    visuals = visual_signals(frames)
    sigs = {s.frame_id: s for s in frame_signals(frames)}

    motions = [visuals[f.id].motion if f.id in visuals else 0.0 for f in frames]
    if smooth > 1:
        half = smooth // 2
        motions = [
            statistics.median(motions[max(0, i - half) : i + half + 1])
            for i in range(len(motions))
        ]

    def regime_of(i: int) -> str:
        f = frames[i]
        busy = "busy" if motions[i] >= motion_split else "calm"
        ctx = f.context if f.context != "(no context)" else "unknown-context"
        src = sigs[f.id].text_source or "none"
        return f"{busy}|{ctx}|{src}"

    out: list[list[Frame]] = []
    current: list[Frame] = []
    current_regime: str | None = None
    for i, f in enumerate(frames):
        gap = (f.timestamp - frames[i - 1].timestamp).total_seconds() if i else 0.0
        regime = regime_of(i)
        too_long = (
            current
            and (f.timestamp - current[0].timestamp).total_seconds()
            > target_minutes * 60
        )
        # Only cut on a regime change if it holds: a single frame of another app is
        # an alt-tab, and a single busy frame is a scroll, not a new activity.
        persists = regime != current_regime and all(
            regime_of(j) != current_regime
            for j in range(i, min(len(frames), i + persist_frames))
        )
        long_enough = len(current) >= min_regime_frames
        if current and (gap >= idle_gap_s or too_long or (persists and long_enough)):
            out.append(current)
            current = []
        current.append(f)
        current_regime = regime
    if current:
        out.append(current)
    return [r for r in out if len(r) >= 2]


def _fixed_windows(frames: list[Frame], minutes: float = 12.0) -> list[list[Frame]]:
    """Equal-length windows.

    Included as the honest alternative to signal regimes. Measured on this
    archive, no deterministic key segments the stream into activity-sized units:
    motion alone yields 103 stretches for an 11-hour day, application name 90,
    and both flap at the sampling rate rather than at activity boundaries. Once
    that is true, an equal window is the same thing without the pretence -- and it
    guarantees coverage, which a signal that flaps does not.
    """
    if not frames:
        return []
    out: list[list[Frame]] = []
    current: list[Frame] = [frames[0]]
    edge = frames[0].timestamp
    for prev, f in zip(frames, frames[1:]):
        gap = (f.timestamp - prev.timestamp).total_seconds()
        elapsed = (f.timestamp - edge).total_seconds() / 60
        if gap >= 240.0 or elapsed >= minutes:
            out.append(current)
            current = []
            edge = f.timestamp
        current.append(f)
    if current:
        out.append(current)
    return [w for w in out if len(w) >= 2]


def _signal_note(chunk: list[Frame], visuals, sigs) -> str:
    motions = [visuals[f.id].motion for f in chunk if f.id in visuals]
    churn = [sigs[f.id].churn for f in chunk if f.id in sigs]
    still = [visuals[f.id].stillness for f in chunk if f.id in visuals]
    ocr = sum(1 for f in chunk if f.text_source == "ocr")
    acc = sum(1 for f in chunk if f.text_source == "accessibility")
    return json.dumps(
        {
            "mean_visual_motion": (
                round(sum(motions) / len(motions), 4) if motions else None
            ),
            "max_still_run": max(still) if still else None,
            "mean_text_churn": round(sum(churn) / len(churn), 2) if churn else None,
            "ocr_frames": ocr,
            "accessibility_frames": acc,
            "contexts": sorted({f.context for f in chunk})[:5],
        }
    )


def run(
    start: str,
    end: str,
    model: str = "gpt-5.4-mini",
    effort: str = "low",
    escalate_model: str | None = None,
    image_budget: int = 24,
    max_asks: int = 3,
    max_chars: int = 40_000,
    prior_window: int = 8,
    smooth: int = 9,
    persist_frames: int = 6,
    target_minutes: float = 8.0,
    regime_mode: str = "fixed",
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)
    frames = archive.frames(start, end)
    visuals = visual_signals(frames)
    sigs = {s.frame_id: s for s in frame_signals(frames)}

    if regime_mode == "fixed":
        regimes = _fixed_windows(frames, minutes=target_minutes)
    else:
        regimes = _regimes(
            frames,
            smooth=smooth,
            persist_frames=persist_frames,
            target_minutes=target_minutes,
        )
    events: list[Event] = []
    trace: list[dict] = []
    images_used = 0
    text_frames = 0

    for index, chunk in enumerate(regimes):
        prior = (
            "\n".join(
                f"[{i}] {e.event_type} {e.started_at} -> {e.ended_at or 'open'}: {e.title}"
                f" (outcome={e.outcome}, status={e.status})"
                for i, e in list(enumerate(events))[-prior_window:]
            )
            or "(none yet)"
        )

        text = compact_text(chunk)[:max_chars]
        text_frames += len(chunk)
        payload = llm.json_complete(
            EXTRACT_PROMPT.format(
                rules=EXTRACTION_RULES,
                index=index,
                start=chunk[0].timestamp.isoformat(),
                end=chunk[-1].timestamp.isoformat(),
                minutes=(chunk[-1].timestamp - chunk[0].timestamp).total_seconds() / 60,
                n=len(chunk),
                signals=_signal_note(chunk, visuals, sigs),
                prior=prior,
                text=text,
                schema=EVENT_SCHEMA,
                max_asks=max_asks,
            )
        )
        if not isinstance(payload, dict):
            payload = {}
        found = [
            Event.from_model(e, f"regime-{index}") for e in payload.get("events", [])
        ]
        asks = payload.get("needs_evidence", []) or []
        step = {
            "regime": index,
            "window": [chunk[0].timestamp.isoformat(), chunk[-1].timestamp.isoformat()],
            "frames": len(chunk),
            "events_from_text": len(found),
            "asks": asks,
            "continues": payload.get("continues", []),
            "escalated": False,
        }

        wants_pixels = bool(asks) or any(
            (e.confidence or 0) < 0.7 or not e.outcome for e in found
        )
        if wants_pixels and found and images_used < image_budget:
            requested = [
                int(fid)
                for ask in asks
                for fid in (ask.get("frame_ids") or [])
                if str(fid).isdigit()
            ]
            ranked = [c.frame_id for c in rank_candidates(archive, chunk, top_k=4)]
            wanted: list[int] = []
            for fid in requested + ranked:
                if fid not in wanted and any(f.id == fid for f in chunk):
                    wanted.append(fid)
            wanted = wanted[: max(0, min(4, image_budget - images_used))]

            images, notes = [], []
            for fid in wanted:
                try:
                    images.append(archive.frame_png(fid, 1280))
                except Exception as exc:  # missing chunk, evicted video
                    notes.append(f"[{fid}] pixels unavailable: {exc}")
                    continue
                frame = archive.frame(fid)
                notes.append(
                    f"[{fid} {frame.timestamp:%H:%M:%S}Z] ocr: {frame.text[:500]}"
                    if frame
                    else f"[{fid}] no row"
                )
            if images:
                images_used += len(images)
                revised = llm.json_complete(
                    ESCALATE_PROMPT.format(
                        rules=EXTRACTION_RULES,
                        start=chunk[0].timestamp.isoformat(),
                        end=chunk[-1].timestamp.isoformat(),
                        questions=(
                            json.dumps(asks, indent=2) if asks else "(low confidence)"
                        ),
                        frame_notes="\n".join(notes),
                        provisional=json.dumps([asdict(e) for e in found], indent=2)[
                            :8000
                        ],
                        schema=EVENT_SCHEMA,
                    ),
                    images=images,
                    model=escalate_model or model,
                )
                if isinstance(revised, dict) and revised.get("events"):
                    found = [
                        Event.from_model(e, f"regime-{index}-escalated")
                        for e in revised["events"]
                    ]
                    step["corrections"] = revised.get("corrections", [])
                step["escalated"] = True
                step["images"] = wanted

        events.extend(found)
        trace.append(step)

    # Reconciliation: one pass over the day's events to merge duplicates and
    # close events a later regime resolved.
    if events:
        reconciled = llm.json_complete(
            f"""{EXTRACTION_RULES}

These events were extracted independently from consecutive stretches of one
capture day. Reconcile them: merge entries that are the same event seen twice,
split an entry that is clearly two events, close events a later entry resolved,
drop entries that are not events by the definition above, and correct any that
rule 1 or rule 2 shows are misattributed.

{json.dumps([asdict(e) for e in events], indent=2, default=str)[:120_000]}

Return JSON: {{"events": [{EVENT_SCHEMA}], "changes": ["..."]}}""",
            effort="medium",
        )
        if isinstance(reconciled, dict) and reconciled.get("events"):
            trace.append(
                {"stage": "reconcile", "changes": reconciled.get("changes", [])}
            )
            events = [Event.from_model(e, "reconciled") for e in reconciled["events"]]

    return RunRecord(
        pipeline=f"p2_escalate_{regime_mode}",
        params={
            "model": model,
            "effort": effort,
            "escalate_model": escalate_model or model,
            "image_budget": image_budget,
            "regimes": len(regimes),
            "regime_mode": regime_mode,
            "smooth": smooth,
            "persist_frames": persist_frames,
            "target_minutes": target_minutes,
        },
        events=events,
        usage=llm.usage.summary(),
        trace=trace,
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=text_frames,
        frames_viewed_as_image=images_used,
        notes="Signal regimes, text extraction, pixel escalation where unsure, then reconciliation.",
    )
