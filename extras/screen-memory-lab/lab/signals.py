"""Deterministic, credential-free signals derived from a capture archive.

This is the layer the design doc puts on the capture node: no model, no policy,
no event vocabulary. It answers only "what changed, where, and how much", and
produces compact structures a backend can reason over without receiving the
frame stream.

Two facts about real ScreenPipe data drive the design:

* `app_name` and `window_name` are empty for 36% of frames on this Wayland
  desktop, including every fullscreen game frame, so context cannot be the
  segmentation key.
* `text_source='accessibility'` frames contain background browser tabs and menu
  chrome that were never visible. They are useful for detecting activity but
  dangerous as evidence, so they are flagged and scored down.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .spipe import Frame, jaccard, normalize, tokens

# Menu and chrome boilerplate that the accessibility tree dumps regardless of
# what is on screen. Present in thousands of frames; carries no information.
CHROME_MARKERS = (
    "new tab below",
    "add split view",
    "reload tab",
    "copy link to highlight",
    "inspect accessibility properties",
    "configure audio devices",
    "print selection",
    "zen compact mode",
)


@dataclass
class FrameSignal:
    """Per-frame deterministic features."""

    frame_id: int
    timestamp: datetime
    context: str
    trigger: str
    text_source: str
    chars: int
    token_count: int
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    similarity_prev: float = 0.0
    novelty: float = 0.0  # share of tokens unseen in the recent past
    chrome_ratio: float = 0.0
    gap_seconds: float = 0.0
    is_chrome_dump: bool = False

    @property
    def churn(self) -> float:
        return 1.0 - self.similarity_prev


@dataclass
class Segment:
    """A contiguous stretch of capture the signal layer believes is one activity."""

    index: int
    start: datetime
    end: datetime
    frame_ids: list[int]
    boundary_reason: str
    contexts: dict = field(default_factory=dict)
    novel_tokens: list[str] = field(default_factory=list)
    stable_runs: int = 0
    peak_novelty_frames: list[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    def summary(self) -> dict:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_s": round(self.duration_s),
            "frames": len(self.frame_ids),
            "frame_range": (
                [self.frame_ids[0], self.frame_ids[-1]] if self.frame_ids else []
            ),
            "boundary_reason": self.boundary_reason,
            "contexts": self.contexts,
            "novel_tokens": self.novel_tokens[:40],
            "anchor_frames": self.peak_novelty_frames,
        }


def chrome_ratio(text: str) -> float:
    low = text.lower()
    hits = sum(1 for m in CHROME_MARKERS if m in low)
    return min(1.0, hits / 3.0)


def frame_signals(frames: list[Frame], novelty_window: int = 60) -> list[FrameSignal]:
    """Compute per-frame change features over a capture run."""
    signals: list[FrameSignal] = []
    recent: list[set[str]] = []
    prev_tokens: set[str] = set()
    prev_ts: datetime | None = None

    for f in frames:
        toks = tokens(f.text)
        seen = set().union(*recent) if recent else set()
        sig = FrameSignal(
            frame_id=f.id,
            timestamp=f.timestamp,
            context=f.context,
            trigger=f.capture_trigger,
            text_source=f.text_source,
            chars=len(f.text),
            token_count=len(toks),
            added=toks - prev_tokens,
            removed=prev_tokens - toks,
            similarity_prev=jaccard(toks, prev_tokens),
            novelty=(len(toks - seen) / len(toks)) if toks else 0.0,
            chrome_ratio=chrome_ratio(f.text),
            gap_seconds=(f.timestamp - prev_ts).total_seconds() if prev_ts else 0.0,
        )
        sig.is_chrome_dump = (
            sig.chrome_ratio >= 0.66 and f.text_source == "accessibility"
        )
        signals.append(sig)

        recent.append(toks)
        if len(recent) > novelty_window:
            recent.pop(0)
        prev_tokens = toks
        prev_ts = f.timestamp
    return signals


def segment(
    frames: list[Frame],
    signals: list[FrameSignal] | None = None,
    churn_threshold: float = 0.75,
    sustain: int = 2,
    idle_gap_s: float = 180.0,
    min_frames: int = 3,
) -> list[Segment]:
    """Split a capture run into activity segments using change-point rules.

    A boundary is declared when either:

    * capture went quiet for ``idle_gap_s`` (the machine was idle or asleep), or
    * ``sustain`` consecutive frames each share less than ``1 - churn_threshold``
      of their tokens with their predecessor -- a *sustained* regime change.

    Requiring the change to be sustained is what stops a single alt-tab, a
    transient dialog, or one bad OCR pass from cutting a segment. A single
    high-churn frame inside otherwise stable activity is kept as an *anchor*
    candidate instead of becoming a boundary, because that is precisely the shape
    of a result screen appearing for a few seconds.
    """
    signals = signals or frame_signals(frames)
    by_id = {f.id: f for f in frames}

    boundaries: list[tuple[int, str]] = [(0, "run start")]
    streak = 0
    for i, sig in enumerate(signals):
        if i == 0:
            continue
        if sig.gap_seconds >= idle_gap_s:
            boundaries.append((i, f"capture gap {int(sig.gap_seconds)}s"))
            streak = 0
            continue
        if sig.churn >= churn_threshold and not sig.is_chrome_dump:
            streak += 1
            if streak >= sustain:
                start = i - streak + 1
                if not boundaries or start > boundaries[-1][0] + min_frames:
                    boundaries.append((start, f"sustained text change x{streak}"))
                streak = 0
        else:
            streak = 0

    segments: list[Segment] = []
    edges = [b[0] for b in boundaries] + [len(signals)]
    for n, (lo, hi) in enumerate(zip(edges, edges[1:])):
        window = signals[lo:hi]
        if not window:
            continue
        frame_ids = [s.frame_id for s in window]
        seg = Segment(
            index=n,
            start=window[0].timestamp,
            end=window[-1].timestamp,
            frame_ids=frame_ids,
            boundary_reason=boundaries[n][1],
        )
        contexts: dict[str, int] = {}
        for fid in frame_ids:
            ctx = by_id[fid].context
            contexts[ctx] = contexts.get(ctx, 0) + 1
        seg.contexts = dict(sorted(contexts.items(), key=lambda kv: -kv[1])[:6])
        seg.novel_tokens = _segment_novel_tokens(window)
        seg.stable_runs = sum(1 for s in window if s.similarity_prev > 0.9)
        seg.peak_novelty_frames = [
            s.frame_id for s in sorted(window, key=lambda s: -s.novelty)[:5]
        ]
        segments.append(seg)
    return segments


def _segment_novel_tokens(window: list[FrameSignal], limit: int = 60) -> list[str]:
    counts: dict[str, int] = {}
    for sig in window:
        if sig.is_chrome_dump:
            continue
        for tok in sig.added:
            counts[tok] = counts.get(tok, 0) + 1
    # Prefer tokens that recur (real content) over one-off OCR garbage, but keep
    # them ordered by first appearance so the list reads chronologically.
    ranked = [
        t for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2
    ]
    return ranked[:limit]


# ------------------------------------------------------------------- anchors


@dataclass
class Anchor:
    """A frame the signal layer thinks is worth a closer look."""

    frame_id: int
    timestamp: datetime
    score: float
    reasons: list[str]
    context: str
    preview: str

    def summary(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "utc": self.timestamp.isoformat(),
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "context": self.context,
            "preview": self.preview[:220],
        }


def anchors(
    frames: list[Frame],
    signals: list[FrameSignal] | None = None,
    top_k: int = 40,
    transient_window: int = 6,
) -> list[Anchor]:
    """Rank frames by how likely they are to carry a state change.

    The scoring is deliberately domain-blind. It rewards:

    * transience -- text that appears, persists for a few frames, then goes away,
      which is the shape of a result screen, a confirmation, or an error;
    * novelty against the recent past;
    * bracketing -- a change that ends a long stable run and precedes another;
    * OCR provenance, since accessibility dumps describe things never displayed.

    It deliberately does not reward text length, which is what the current
    Chronicle candidate ranker does and why it prefers ordinary dashboards.
    """
    signals = signals or frame_signals(frames)
    by_id = {f.id: f for f in frames}
    scored: list[Anchor] = []

    for i, sig in enumerate(signals):
        if sig.is_chrome_dump or sig.token_count < 4:
            continue
        reasons: list[str] = []
        score = 0.0

        if sig.novelty > 0.5:
            score += 1.5 * sig.novelty
            reasons.append(f"novel text {sig.novelty:.0%}")

        # Transience: how much of this frame's newly added text is gone again
        # within the next few frames.
        future = signals[i + 1 : i + 1 + transient_window]
        if sig.added and future:
            survives = (
                set().union(*[f.added | (f.added or set()) for f in future])
                if future
                else set()
            )
            still_present = 0
            for f in future:
                # A token is "still present" if the later frame did not report it
                # as added and did report it as removed at some point.
                if sig.added & f.removed:
                    still_present += 1
            if still_present:
                score += 1.0
                reasons.append("text appeared then disappeared")

        # Bracketing: a stable run before and after this frame.
        before = signals[max(0, i - 6) : i]
        stable_before = sum(1 for s in before if s.similarity_prev > 0.85)
        stable_after = sum(1 for s in future if s.similarity_prev > 0.85)
        if stable_before >= 2 and stable_after >= 2 and sig.churn > 0.5:
            score += 1.2
            reasons.append("change between two stable states")

        if sig.text_source == "ocr":
            score += 0.4
            reasons.append("ocr of visible pixels")
        elif sig.text_source == "accessibility":
            score -= 0.5
            reasons.append("accessibility text (may include hidden content)")

        if sig.gap_seconds > 120:
            score += 0.4
            reasons.append(f"first frame after {int(sig.gap_seconds)}s quiet")

        if score <= 0:
            continue
        frame = by_id[sig.frame_id]
        scored.append(
            Anchor(
                frame_id=sig.frame_id,
                timestamp=sig.timestamp,
                score=score,
                reasons=reasons,
                context=frame.context,
                preview=normalize(frame.text)[:220],
            )
        )

    scored.sort(key=lambda a: -a.score)
    return scored[:top_k]


# -------------------------------------------------------------- day digests


def timeline_digest(
    frames: list[Frame],
    bucket_minutes: int = 5,
    tokens_per_bucket: int = 14,
) -> list[dict]:
    """A compact, model-readable index of a capture period.

    One row per bucket: how much was captured, what context dominated, and the
    tokens that are new relative to everything seen so far. This is the cheap
    artifact a backend can hold for a whole day -- roughly 80 tokens per bucket
    instead of tens of thousands for the raw text.
    """
    if not frames:
        return []
    signals = frame_signals(frames)
    by_id = {f.id: f for f in frames}
    seen: set[str] = set()
    rows: list[dict] = []

    start = frames[0].timestamp.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % bucket_minutes)
    step = timedelta(minutes=bucket_minutes)

    bucket: list[FrameSignal] = []
    edge = start + step
    for sig in signals:
        while sig.timestamp >= edge:
            if bucket:
                rows.append(
                    _digest_row(
                        bucket, by_id, seen, tokens_per_bucket, edge - step, edge
                    )
                )
            bucket = []
            edge += step
        bucket.append(sig)
    if bucket:
        rows.append(
            _digest_row(bucket, by_id, seen, tokens_per_bucket, edge - step, edge)
        )
    return rows


def _digest_row(bucket, by_id, seen, tokens_per_bucket, lo, hi) -> dict:
    contexts: dict[str, int] = {}
    fresh: dict[str, int] = {}
    for sig in bucket:
        frame = by_id[sig.frame_id]
        ctx = frame.context
        contexts[ctx] = contexts.get(ctx, 0) + 1
        if sig.is_chrome_dump:
            continue
        for tok in tokens(frame.text) - seen:
            fresh[tok] = fresh.get(tok, 0) + 1

    ranked = [
        t for t, c in sorted(fresh.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2
    ]
    seen.update(fresh)
    churn = [s.churn for s in bucket]
    return {
        "from": lo.isoformat(),
        "to": hi.isoformat(),
        "frames": len(bucket),
        "frame_range": [bucket[0].frame_id, bucket[-1].frame_id],
        "context": max(contexts.items(), key=lambda kv: kv[1])[0] if contexts else "",
        "contexts": len(contexts),
        "mean_churn": round(statistics.fmean(churn), 2) if churn else 0.0,
        "ocr_frames": sum(1 for s in bucket if s.text_source == "ocr"),
        "chrome_frames": sum(1 for s in bucket if s.is_chrome_dump),
        "new_tokens": ranked[:tokens_per_bucket],
    }


def compact_text(frames: list[Frame], max_chars_per_frame: int = 400) -> str:
    """Deduplicated OCR text for a set of frames, oldest first.

    Consecutive frames whose normalized text is nearly identical collapse into
    one line with a repeat count, which is what makes a 600-frame gameplay
    stretch fit in a prompt at all.
    """
    lines: list[str] = []
    prev: set[str] = set()
    repeat = 0
    for f in frames:
        toks = tokens(f.text)
        if prev and jaccard(toks, prev) > 0.8:
            repeat += 1
            continue
        if repeat:
            lines.append(f"    (+{repeat} near-identical frames)")
            repeat = 0
        flag = "" if f.text_source == "ocr" else f" [{f.text_source or 'no-source'}]"
        text = re.sub(r"\s+", " ", f.text)[:max_chars_per_frame]
        lines.append(f"[{f.id} {f.timestamp:%H:%M:%S}Z]{flag} {text}")
        prev = toks
    if repeat:
        lines.append(f"    (+{repeat} near-identical frames)")
    return "\n".join(lines)


def entropy(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts.values() if c)
