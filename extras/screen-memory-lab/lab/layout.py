"""Typographic salience: which text on a screen was *presented* as important.

ScreenPipe's `elements` table stores every OCR word with a normalized bounding
box and a confidence. That is enough to recover something applications do
universally and Chronicle currently ignores: they announce state changes in large
type.

"Victory". "Defeat". "Order placed". "Build failed". "Payment received". The
words differ per domain; the typography does not. Ranking a frame by the size of
its largest confidently-read text is therefore a domain-blind way to find the
frames that announce something -- no vocabulary, no per-application rules, no
model call.

Two honest limits, measured on this archive:

* `elements` rows exist for only 51% of the OCR frames on the evaluation day, so
  this signal is a booster, not a backbone. Frames without rows fall back to
  text-shape heuristics.
* It ranks a menu title ("Multiplayer") as highly as an outcome ("Defeat"), since
  both are large type. That is the right behaviour for a signal layer that is not
  supposed to know what things mean -- discriminating between them is the
  backend's job, and it is cheap once the candidate set is 20 frames instead of
  3000.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .spipe import Archive

MIN_CONFIDENCE = 75.0
MIN_LENGTH = 3
_WORDY = re.compile(r"[A-Za-z]{3,}")


@dataclass
class Salience:
    frame_id: int
    top_text: str
    top_height: float
    top_confidence: float
    banner_texts: list  # all confident text within 85% of the tallest
    centrality: float  # 0..1, how horizontally centred the tallest text is
    upper_third: bool
    element_rows: int

    @property
    def score(self) -> float:
        """Height weighted by confidence, with a small bonus for centred text."""
        base = self.top_height * (self.top_confidence / 100.0)
        return base * (1.0 + 0.3 * self.centrality)

    def summary(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "top_text": self.top_text,
            "height": round(self.top_height, 4),
            "confidence": round(self.top_confidence),
            "score": round(self.score, 5),
            "banner": self.banner_texts[:6],
            "centred": round(self.centrality, 2),
        }


def salience(archive: Archive, frame_id: int) -> Salience | None:
    """Typographic salience for one frame, or None if it has no element rows."""
    rows = archive.conn.execute(
        """
        SELECT text, height_bound, confidence, left_bound, width_bound, top_bound
        FROM elements
        WHERE frame_id = ? AND text IS NOT NULL AND source = 'ocr'
        """,
        (frame_id,),
    ).fetchall()
    if not rows:
        return None

    usable = [
        r
        for r in rows
        if (r["confidence"] or 0) >= MIN_CONFIDENCE
        and len(r["text"].strip()) >= MIN_LENGTH
        and _WORDY.search(r["text"])
    ]
    if not usable:
        return Salience(frame_id, "", 0.0, 0.0, [], 0.0, False, len(rows))

    top = max(usable, key=lambda r: r["height_bound"] or 0)
    height = top["height_bound"] or 0.0
    banner = [
        r["text"]
        for r in sorted(usable, key=lambda r: -(r["height_bound"] or 0))
        if (r["height_bound"] or 0) >= height * 0.85
    ]
    centre = (top["left_bound"] or 0) + (top["width_bound"] or 0) / 2
    return Salience(
        frame_id=frame_id,
        top_text=top["text"].strip(),
        top_height=height,
        top_confidence=top["confidence"] or 0.0,
        banner_texts=banner,
        centrality=max(0.0, 1.0 - abs(centre - 0.5) * 2),
        upper_third=(top["top_bound"] or 1.0) < 0.34,
        element_rows=len(rows),
    )


def salience_map(archive: Archive, frame_ids: list[int]) -> dict[int, Salience]:
    """Salience for many frames in one query pass."""
    if not frame_ids:
        return {}
    out: dict[int, Salience] = {}
    marks = ",".join("?" * len(frame_ids))
    rows = archive.conn.execute(
        f"""
        SELECT frame_id, text, height_bound, confidence, left_bound, width_bound, top_bound
        FROM elements
        WHERE frame_id IN ({marks}) AND text IS NOT NULL AND source = 'ocr'
        """,
        frame_ids,
    ).fetchall()

    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["frame_id"], []).append(r)

    for fid, group in grouped.items():
        usable = [
            r
            for r in group
            if (r["confidence"] or 0) >= MIN_CONFIDENCE
            and len(r["text"].strip()) >= MIN_LENGTH
            and _WORDY.search(r["text"])
        ]
        if not usable:
            out[fid] = Salience(fid, "", 0.0, 0.0, [], 0.0, False, len(group))
            continue
        top = max(usable, key=lambda r: r["height_bound"] or 0)
        height = top["height_bound"] or 0.0
        banner = [
            r["text"]
            for r in sorted(usable, key=lambda r: -(r["height_bound"] or 0))
            if (r["height_bound"] or 0) >= height * 0.85
        ]
        centre = (top["left_bound"] or 0) + (top["width_bound"] or 0) / 2
        out[fid] = Salience(
            frame_id=fid,
            top_text=top["text"].strip(),
            top_height=height,
            top_confidence=top["confidence"] or 0.0,
            banner_texts=banner,
            centrality=max(0.0, 1.0 - abs(centre - 0.5) * 2),
            upper_third=(top["top_bound"] or 1.0) < 0.34,
            element_rows=len(group),
        )
    return out


def banner_transience(
    sal: dict[int, Salience],
    ordered_ids: list[int],
    window: int = 8,
) -> dict[int, float]:
    """How unusual each frame's banner text is against its neighbours.

    A banner that is on screen for a couple of frames and then gone is a state
    announcement. A banner that is on screen all day is a window title.
    """
    out: dict[int, float] = {}
    for i, fid in enumerate(ordered_ids):
        me = sal.get(fid)
        if not me or not me.top_text:
            continue
        lo, hi = max(0, i - window), min(len(ordered_ids), i + window + 1)
        neighbours = [
            sal[n].top_text
            for n in ordered_ids[lo:hi]
            if n != fid and n in sal and sal[n].top_text
        ]
        if not neighbours:
            out[fid] = 1.0
            continue
        same = sum(1 for t in neighbours if t.lower() == me.top_text.lower())
        out[fid] = 1.0 - (same / len(neighbours))
    return out
