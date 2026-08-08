"""Build a contact sheet of candidate frames, for looking at many at once.

Frame 7173 -- the full-screen DEFEAT banner that ScreenPipe stored zero OCR
characters for -- has a structural signature that needs no text at all:
fullscreen (no app name reported), `capture_trigger=visual_change`, and empty
stored text. This renders every frame matching that signature so the precision of
the signature can be judged by eye instead of asserted.

That matters because the two text-based candidate rankers both failed on exactly
this frame: typographic salience cannot rank it (no OCR means no `elements` rows)
and text-length ranking sees an empty string.

Run:
    uv run python contact_sheet.py --since 2026-07-25T16:00:00+00:00
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from lab.spipe import open_archive
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "out" / "sheets"
TILE_W = 420
COLS = 4


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="UTC ISO lower bound")
    ap.add_argument("--until", default=None)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--per-episode", type=int, default=3)
    ap.add_argument("--name", default="sheet")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    arc = open_archive()
    lo, hi = arc.span()
    frames = arc.frames(
        args.since or lo.isoformat(),
        args.until or (hi + timedelta(seconds=1)).isoformat(),
    )

    sig = [
        f
        for f in frames
        if not f.app_name
        and not (f.text or "").strip()
        and f.capture_trigger == "visual_change"
        and f.chunk_path
    ]
    # Sample per episode rather than taking the first N, so a single long episode
    # cannot crowd out the rest of the day.
    episodes: list[list] = []
    for f in sig:
        if episodes and f.timestamp - episodes[-1][-1].timestamp <= timedelta(
            minutes=4
        ):
            episodes[-1].append(f)
        else:
            episodes.append([f])
    picks = []
    for ep in episodes:
        step = max(1, len(ep) // args.per_episode)
        picks.extend(ep[::step][: args.per_episode])
    picks = picks[: args.max]
    print(
        f"{len(sig)} signature frames in {len(episodes)} episodes; showing {len(picks)}"
    )

    tiles = []
    for f in picks:
        try:
            src = arc.frame_png(f.id, max_width=TILE_W)
        except Exception as exc:  # noqa: BLE001
            print(f"  f{f.id}: {exc}")
            continue
        im = Image.open(src).convert("RGB")
        im.thumbnail((TILE_W, TILE_W))
        card = Image.new("RGB", (TILE_W, im.height + 22), (18, 20, 26))
        card.paste(im, (0, 22))
        ImageDraw.Draw(card).text(
            (5, 5), f"f{f.id}  {f.local_time:%m-%d %H:%M:%S}", fill=(210, 215, 225)
        )
        tiles.append(card)

    if not tiles:
        print("nothing to render")
        return
    rows = (len(tiles) + COLS - 1) // COLS
    h = max(t.height for t in tiles)
    sheet = Image.new("RGB", (COLS * TILE_W, rows * h), (10, 11, 14))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % COLS) * TILE_W, (i // COLS) * h))
    dest = OUT / f"{args.name}.png"
    sheet.save(dest)
    print(f"wrote {dest} ({sheet.width}x{sheet.height}, {len(tiles)} frames)")


if __name__ == "__main__":
    main()
