"""Export screen frames as PNGs plus a manifest, for VLM benchmarking.

Two sets are exported, for two different questions.

**Systematic set** -- the frame nearest each point on a fixed time grid. This is
what a collector running at an adjustable frequency would actually send, so it
answers "at 1 frame per N seconds, does the model see the events at all". It is
chosen by the clock, never by content, so it cannot be cherry-picked to flatter
the model.

**Targeted set** -- the frames a human verified as decisive for the ground-truth
day, plus the trap frames. This answers the separate question "given that the
decisive frame is in front of it, does the model read it correctly". Mixing
these two questions is how you get a benchmark that looks good and means
nothing.

The manifest carries each frame's stored OCR text so the VLM output can be
compared against the text-only baseline the existing prototypes used.

Run:
    uv run python export_frames.py --every 600 --out out/frames/grid600
    uv run python export_frames.py --targeted --out out/frames/targeted
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab.spipe import open_archive

GT_START = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
GT_END = datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc)

# Frames a human verified while building ground truth: the two captured match
# result screens, the surrender moments, the opponent-name frame, and the five
# traps. Each is labelled with what a correct reading would say, so scoring does
# not depend on my memory of what the frame showed.
TARGETED = [
    (6871, "m1 pre-game menu, Multiplayer screen"),
    (6900, "m1 gameplay, Marshland"),
    (7050, "m1 in progress"),
    (7130, "m1 aftermath / post-surrender, opponent WLD6116 surrendered"),
    (7152, "m2 menu, map exclusion list visible - TRAP: not a map choice"),
    (7158, "m2 menu Multiplayer"),
    (7159, "m2 lobby, opponent XRaptoR72 visible"),
    (7160, "m2 Quick match search"),
    (8280, "m3 pre-game, Legacy/civ screen"),
    (8294, "m3 map or biome screen - TRAP: ALPINE SPRING is a biome, not the map"),
    (8302, "m3 Multiplayer"),
    (8577, "m3 result screen: DEFEAT vs Ibar on Golden Pit"),
    (8581, "m3 result screen DEFEAT"),
    (8981, "m4 result screen: VICTORY vs King Maximilian on Himeyama"),
    (9048, "assistant text about matches - TRAP: not gameplay evidence"),
    (9200, "assistant text about matches - TRAP: not gameplay evidence"),
    (9652, "assistant text about matches - TRAP: not gameplay evidence"),
]


def nearest_with_pixels(frames, target):
    """The frame closest to `target` that has a video chunk behind it."""
    best, best_gap = None, None
    for f in frames:
        if not f.chunk_path:
            continue
        gap = abs((f.timestamp - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = f, gap
    return best, best_gap


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=600, help="grid interval, seconds")
    ap.add_argument("--targeted", action="store_true", help="export the verified set")
    ap.add_argument(
        "--ids",
        default="",
        help="comma-separated frame ids to export instead of a grid, for probing "
        "specific known frames (zero-OCR banners, traps) alongside a grid",
    )
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--start", default=GT_START.isoformat())
    ap.add_argument("--end", default=GT_END.isoformat())
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "png").mkdir(parents=True, exist_ok=True)
    arc = open_archive()

    frames = arc.frames(args.start, args.end)
    by_id = {f.id: f for f in frames}
    print(f"{len(frames)} frames in {args.start} -> {args.end}")

    picks: list[tuple] = []
    if args.ids:
        for raw in args.ids.split(","):
            fid = int(raw.strip())
            f = by_id.get(fid) or arc.frame(fid)
            if f is None:
                print(f"  frame {fid} not in archive, skipped")
                continue
            picks.append((f, "explicitly requested", None))
    elif args.targeted:
        for fid, note in TARGETED:
            f = by_id.get(fid) or arc.frame(fid)
            if f is None:
                print(f"  frame {fid} not in archive, skipped")
                continue
            picks.append((f, note, None))
    else:
        start = datetime.fromisoformat(args.start)
        end = datetime.fromisoformat(args.end)
        t = start
        while t < end:
            lo = t - timedelta(seconds=args.every / 2)
            hi = t + timedelta(seconds=args.every / 2)
            window = [f for f in frames if lo <= f.timestamp <= hi]
            f, gap = nearest_with_pixels(window, t)
            if f is not None:
                picks.append((f, None, gap))
            t += timedelta(seconds=args.every)

    manifest = []
    for i, (f, note, gap) in enumerate(picks):
        try:
            png = arc.frame_png(f.id, max_width=args.width)
        except Exception as exc:  # noqa: BLE001 - want the reason in the manifest
            print(f"  frame {f.id}: no pixels ({exc})")
            manifest.append({"frame_id": f.id, "png": None, "error": str(exc)[:200]})
            continue
        dest = out / "png" / f"{i:04d}_f{f.id}.png"
        dest.write_bytes(png.read_bytes())
        manifest.append(
            {
                "seq": i,
                "frame_id": f.id,
                "png": dest.name,
                "utc": f.timestamp.isoformat(),
                "local": f.local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "app": f.app_name or "",
                "window": f.window_name or "",
                "text_source": f.text_source,
                "grid_gap_seconds": round(gap, 1) if gap is not None else None,
                "human_note": note,
                "ocr_text": " ".join(f.text.split())[:2000],
            }
        )

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    ok = [m for m in manifest if m.get("png")]
    mb = sum((out / "png" / m["png"]).stat().st_size for m in ok) / 1e6
    print(f"exported {len(ok)}/{len(manifest)} frames, {mb:.1f} MB -> {out}")
    if not args.targeted and ok:
        gaps = [m["grid_gap_seconds"] for m in ok if m["grid_gap_seconds"] is not None]
        if gaps:
            print(
                f"grid alignment: median gap {sorted(gaps)[len(gaps)//2]:.0f}s, "
                f"max {max(gaps):.0f}s (interval {args.every}s)"
            )


if __name__ == "__main__":
    main()
