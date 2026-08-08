"""Choosing which frames are worth a closer look.

This is the part of the signal layer that decides what evidence to offer a model,
and `bench_anchors.py` measures it directly. On the evaluation day, ranking 2821
frames by:

* text length, which is what Chronicle's candidate ranker does today, puts every
  decisive frame between rank 1667 and 2627 -- worse than random, because result
  screens carry *short* text;
* typographic salience puts the two result screens at ranks 7 and 9.

So the ranking function is not a detail. It is the difference between a backend
that sees the outcome and one that sees a mid-game frame.
"""

from __future__ import annotations

from dataclasses import dataclass

from .layout import banner_transience, salience_map
from .signals import frame_signals
from .spipe import Archive, Frame, tokens
from .visual import visual_signals


@dataclass
class Candidate:
    frame_id: int
    score: float
    utc: str
    banner: str
    why: str
    context: str
    preview: str

    def summary(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "utc": self.utc,
            "score": round(self.score, 3),
            "banner_text": self.banner,
            "why": self.why,
            "context": self.context,
            "preview": self.preview[:200],
        }


def rank_candidates(
    archive: Archive,
    frames: list[Frame],
    top_k: int = 20,
    include_fallback: bool = True,
) -> list[Candidate]:
    """Rank frames by how likely they are to carry a state change.

    Primary signal is typographic: the tallest confidently-read text on the
    screen, weighted by how unusual that banner is among neighbouring frames. A
    frame whose largest text is the same as its neighbours' is a window title; one
    whose largest text appears briefly and then goes is an announcement.

    Only 51% of OCR frames have their own layout rows, so ``include_fallback``
    scores the rest from text shape and visual settling. Layout rows are never
    read through ``frames.elements_ref_frame_id``: on this archive those borrowed
    rows agree with the frame's own OCR at a median Jaccard of 0.16, so they
    describe a different screen 76% of the time.
    """
    ids = [f.id for f in frames]
    sal = salience_map(archive, ids)
    trans = banner_transience(sal, ids)
    vis = visual_signals(frames)
    sigs = {s.frame_id: s for s in frame_signals(frames)}
    by_id = {f.id: f for f in frames}

    out: list[Candidate] = []
    for fid in ids:
        frame = by_id[fid]
        sig = sigs[fid]
        if sig.is_chrome_dump or frame.text_source != "ocr":
            continue
        s = sal.get(fid)
        reasons = []
        if s and s.top_text:
            score = s.score * 100
            reasons.append(
                f"largest text {s.top_text!r} at {s.top_height:.3f} of screen height"
            )
            t = trans.get(fid, 0.0)
            score *= 1.0 + t
            if t > 0.5:
                reasons.append("banner differs from neighbouring frames")
            banner = " ".join(s.banner_texts[:5])
        elif include_fallback:
            toks = tokens(frame.text)
            if not (2 <= len(toks) <= 30):
                continue
            v = vis.get(fid)
            settled = 1.4 if (v and v.stillness >= 1) else 1.0
            score = (0.35 + 0.9 * sig.novelty) * settled
            reasons.append(
                "short burst of mostly-new text (no layout rows for this frame)"
            )
            if settled > 1:
                reasons.append("screen had stopped changing")
            banner = ""
        else:
            continue
        out.append(
            Candidate(
                frame_id=fid,
                score=score,
                utc=frame.timestamp.isoformat(),
                banner=banner,
                why="; ".join(reasons),
                context=frame.context,
                preview=" ".join(frame.text.split())[:200],
            )
        )

    out.sort(key=lambda c: -c.score)
    return out[:top_k]
