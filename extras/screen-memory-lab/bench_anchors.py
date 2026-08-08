"""How well can a credential-free signal layer surface the frames that matter?

Chronicle's backend can only reason about evidence the collector chose to send.
This benchmark asks, for four candidate rankers, where the hand-verified decisive
frames land in the ranking of a whole capture day.

The decisive frames are the ones a human needed in order to establish the day's
events: the two result screens, the surrender toast and its chat line, and the two
lobby screens that name the map and opponent. They are 8 frames out of 2821.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.layout import banner_transience, salience_map  # noqa: E402
from lab.signals import anchors, frame_signals  # noqa: E402
from lab.spipe import open_archive, tokens  # noqa: E402
from lab.visual import visual_signals  # noqa: E402

DECISIVE = {
    7064: "m1 victory result",
    7066: "m1 result timeline (duration)",
    7171: "m2 surrender chat + reason",
    7172: "m2 surrender confirmation",
    8306: "m3 lobby (map, opponent)",
    8577: "m3 defeat result",
    8591: "m4 lobby (map, opponent)",
    8981: "m4 victory result (duration)",
}


def rank_by_text_length(frames, archive):
    """Approximates Chronicle's current candidate ranker: longer text wins."""
    scored = [(len(f.text), f.id) for f in frames]
    scored.sort(reverse=True)
    return [fid for _, fid in scored]


def rank_by_heuristic_anchors(frames, archive):
    """The transience/novelty/bracketing heuristic in lab.signals."""
    return [a.frame_id for a in anchors(frames, top_k=len(frames))]


def rank_by_typographic_salience(frames, archive):
    """Largest confidently-read text, weighted by confidence and centring."""
    sal = salience_map(archive, [f.id for f in frames])
    scored = [(s.score, fid) for fid, s in sal.items() if s.top_text]
    scored.sort(reverse=True)
    return [fid for _, fid in scored]


def rank_salience_x_transience(frames, archive):
    """Salience weighted by how unusual the banner is among its neighbours."""
    ids = [f.id for f in frames]
    sal = salience_map(archive, ids)
    trans = banner_transience(sal, ids)
    scored = [
        (s.score * (1.0 + trans.get(fid, 0.0)), fid)
        for fid, s in sal.items()
        if s.top_text
    ]
    scored.sort(reverse=True)
    return [fid for _, fid in scored]


def rank_combined(frames, archive):
    """Salience x transience, with a text-shape fallback where layout is missing.

    Half the OCR frames have no element rows, so pure salience cannot rank them at
    all. The fallback gives those frames the text-only shadow of a banner: a short
    burst of high-novelty text on a visually settled screen.
    """
    ids = [f.id for f in frames]
    sal = salience_map(archive, ids)
    trans = banner_transience(sal, ids)
    vis = visual_signals(frames)
    sigs = {s.frame_id: s for s in frame_signals(frames)}
    by_id = {f.id: f for f in frames}

    scored = []
    for fid in ids:
        frame = by_id[fid]
        sig = sigs[fid]
        if sig.is_chrome_dump or frame.text_source != "ocr":
            continue
        s = sal.get(fid)
        if s and s.top_text:
            score = s.score * 100 * (1.0 + trans.get(fid, 0.0))
        else:
            toks = tokens(frame.text)
            if not (2 <= len(toks) <= 30):
                continue
            v = vis.get(fid)
            settled = 1.4 if (v and v.stillness >= 1) else 1.0
            # Short text that is mostly new, on a screen that has stopped moving.
            score = (0.35 + 0.9 * sig.novelty) * settled
        scored.append((score, fid))
    scored.sort(reverse=True)
    return [fid for _, fid in scored]


RANKERS = {
    "text length (current Chronicle)": rank_by_text_length,
    "transience heuristic": rank_by_heuristic_anchors,
    "typographic salience": rank_by_typographic_salience,
    "salience x transience": rank_salience_x_transience,
    "combined + text fallback": rank_combined,
}

# What each match needs: any one of these frames is enough to establish its
# outcome, so per-event recall is the number that matters for a signal layer.
PER_EVENT = {
    "m1 victory vs WLD6116": [7063, 7064, 7066],
    "m2 surrender on Mountain Clearing": [7171, 7172],
    "m3 defeat vs Ibar": [8572, 8577, 8578, 8579, 8580, 8581, 8582, 8583],
    "m4 victory vs King Maximilian": [8975, 8981],
}


def main() -> None:
    archive = open_archive()
    frames = archive.frames("2026-07-24T14:00:00+00:00", "2026-07-25T01:00:00+00:00")
    print(
        f"{len(frames)} frames in the evaluation day; {len(DECISIVE)} decisive frames\n"
    )

    header = f"{'ranker':<34}" + "".join(
        f"{'@' + str(k):>7}" for k in (10, 20, 50, 100, 200)
    )
    print(header)
    print("-" * len(header))
    detail = {}
    for name, fn in RANKERS.items():
        order = fn(frames, archive)
        pos = {fid: i + 1 for i, fid in enumerate(order)}
        row = f"{name:<34}"
        for k in (10, 20, 50, 100, 200):
            hits = sum(1 for fid in DECISIVE if pos.get(fid, 10**9) <= k)
            row += f"{hits}/{len(DECISIVE):>5}"
        print(row)
        detail[name] = pos

    print("\nrank of each decisive frame:")
    print(
        f"{'frame':<8}{'what it proves':<32}"
        + "".join(f"{n[:16]:>18}" for n in RANKERS)
    )
    for fid, what in sorted(DECISIVE.items()):
        row = f"{fid:<8}{what:<32}"
        for name in RANKERS:
            p = detail[name].get(fid)
            row += f"{(str(p) if p else 'unranked'):>18}"
        print(row)

    print("\nper-event recall -- is ANY frame that proves this event in the top K?")
    header = f"{'ranker':<34}" + "".join(
        f"{'@' + str(k):>7}" for k in (10, 20, 50, 100)
    )
    print(header)
    print("-" * len(header))
    for name in RANKERS:
        pos = detail[name]
        row = f"{name:<34}"
        for k in (10, 20, 50, 100):
            hits = sum(
                1
                for _, fids in PER_EVENT.items()
                if any(pos.get(fid, 10**9) <= k for fid in fids)
            )
            row += f"{hits}/{len(PER_EVENT):>5}"
        print(row)

    print("\ntop 20 of the combined ranker:")
    order = rank_combined(frames, archive)
    sal = salience_map(archive, [f.id for f in frames])
    for i, fid in enumerate(order[:20], 1):
        s = sal.get(fid)
        mark = "  <== DECISIVE" if fid in DECISIVE else ""
        frame = archive.frame(fid)
        print(
            f"{i:>3}. frame {fid:<6} {frame.timestamp:%H:%M:%S}Z "
            f"banner={(s.top_text if s else '(no layout rows)')!r:<22}{mark}"
        )


if __name__ == "__main__":
    main()
