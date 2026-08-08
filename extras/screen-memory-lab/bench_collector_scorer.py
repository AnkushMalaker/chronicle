"""Does the collector's candidate scorer pick the frames that prove an event?

The collector chooses one representative frame per observation and ranks
`frame_candidates` for the backend. Whatever that scorer prefers is what the
backend gets to see, so it is worth simulating directly against a day whose
decisive frames are known.

Two scorers are compared: the one on `dev`, and the modified one currently
uncommitted in the working tree, which adds a bonus for accessibility/hybrid text
and a penalty for OCR frames that carry no application context.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.spipe import open_archive  # noqa: E402

MEANINGFUL = {
    "app_switch",
    "window_focus",
    "click",
    "typing_pause",
    "scroll_stop",
    "clipboard",
    "manual",
}
INACTIVE = {"idle", "locked", "blank", "drm_paused"}
STRUCTURED = {"accessibility", "hybrid"}

DECISIVE = {
    7064: "m1 victory result",
    7066: "m1 result timeline",
    7171: "m2 surrender chat",
    7172: "m2 surrender toast",
    8306: "m3 lobby",
    8577: "m3 defeat result",
    8591: "m4 lobby",
    8981: "m4 victory result",
}


def has_context(f) -> bool:
    return bool(f.app_name or f.window_name or f.browser_url)


def contextless_ocr(f) -> bool:
    return f.text_source == "ocr" and not has_context(f)


def score_current(f, frame_count: int = 1) -> float:
    """`_candidate_score` as committed on dev."""
    return (
        min(len(f.text), 1000) / 1000
        + (0.75 if f.capture_trigger in MEANINGFUL else 0)
        + (0.25 if frame_count > 1 else 0)
        - (1.0 if f.capture_trigger in INACTIVE else 0)
    )


def score_working_tree(f, frame_count: int = 1) -> float:
    """`_candidate_score` with the uncommitted changes applied."""
    return (
        score_current(f, frame_count)
        + (0.5 if f.text_source in STRUCTURED else 0)
        - (0.75 if contextless_ocr(f) else 0)
    )


def main() -> None:
    archive = open_archive()
    day = archive.frames("2026-07-24T14:00:00+00:00", "2026-07-25T01:00:00+00:00")

    print(f"{len(day)} frames in the evaluation day, {len(DECISIVE)} decisive\n")
    for label, fn in (
        ("current (dev)", score_current),
        ("working tree", score_working_tree),
    ):
        ranked = sorted(day, key=lambda f: -fn(f))
        pos = {f.id: i + 1 for i, f in enumerate(ranked)}
        ranks = sorted(pos[k] for k in DECISIVE if k in pos)
        best = ranks[0] if ranks else None
        print(f"{label:<16} decisive-frame ranks: {ranks}   best={best}")

    print("\nper-frame effect of the uncommitted change:")
    print(
        f"{'frame':<7}{'what it proves':<22}{'source':<15}{'context':<9}{'current':>9}{'working':>9}{'delta':>8}"
    )
    for fid, what in sorted(DECISIVE.items()):
        f = archive.frame(fid)
        if f is None:
            continue
        cur, new = score_current(f), score_working_tree(f)
        print(
            f"{fid:<7}{what:<22}{(f.text_source or '-'):<15}"
            f"{('yes' if has_context(f) else 'no'):<9}{cur:>9.2f}{new:>9.2f}{new - cur:>+8.2f}"
        )

    penalised = [f for f in day if contextless_ocr(f)]
    rewarded = [f for f in day if f.text_source in STRUCTURED]
    print(
        f"\nframes the new penalty applies to (contextless OCR): "
        f"{len(penalised)}/{len(day)} = {len(penalised)/len(day):.0%}"
    )
    print(
        f"frames the new bonus applies to (accessibility/hybrid): "
        f"{len(rewarded)}/{len(day)} = {len(rewarded)/len(day):.0%}"
    )
    decisive_contextless = [
        k for k in DECISIVE if (fr := archive.frame(k)) and contextless_ocr(fr)
    ]
    print(
        f"decisive frames that are contextless OCR, and so penalised: "
        f"{len(decisive_contextless)}/{len(DECISIVE)} -> {sorted(decisive_contextless)}"
    )


if __name__ == "__main__":
    main()
