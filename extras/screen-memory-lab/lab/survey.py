"""Inspection CLI for a ScreenPipe archive: look at what the signal layer sees.

uv run python -m lab.survey digest  2026-07-24T14:00 2026-07-25T01:00
uv run python -m lab.survey segments 2026-07-24T14:00 2026-07-25T01:00
uv run python -m lab.survey anchors  2026-07-24T14:00 2026-07-25T01:00
uv run python -m lab.survey frames 7171 7172 --image
uv run python -m lab.survey text 2026-07-24T15:27 2026-07-24T15:31
"""

from __future__ import annotations

import argparse
import json

from .signals import anchors, compact_text, frame_signals, segment, timeline_digest
from .spipe import open_archive


def _utc(s: str) -> str:
    return s if "+" in s or s.endswith("Z") else s + "+00:00"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "command", choices=["digest", "segments", "anchors", "frames", "text", "span"]
    )
    ap.add_argument("args", nargs="*")
    ap.add_argument(
        "--image", action="store_true", help="extract PNGs for the named frames"
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bucket", type=int, default=5)
    ap.add_argument("--top", type=int, default=40)
    opts = ap.parse_args()

    archive = open_archive()

    if opts.command == "span":
        lo, hi = archive.span()
        print(f"{lo.isoformat()} .. {hi.isoformat()}")
        return

    if opts.command == "frames":
        for raw in opts.args:
            frame = archive.frame(int(raw))
            if frame is None:
                print(f"{raw}: not found")
                continue
            print(json.dumps(frame.as_row(), indent=2))
            print(frame.text[:2000])
            if opts.image:
                print("image:", archive.frame_png(frame.id, 1600))
            print("-" * 70)
        return

    start, end = _utc(opts.args[0]), _utc(opts.args[1])
    frames = archive.frames(start, end)
    print(f"# {len(frames)} frames in {start} .. {end}")

    if opts.command == "digest":
        rows = timeline_digest(frames, bucket_minutes=opts.bucket)
        if opts.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(
                    f"{r['from'][11:16]} f={r['frames']:>3} ctx={r['contexts']} "
                    f"churn={r['mean_churn']:.2f} ocr={r['ocr_frames']:>3} "
                    f"chrome={r['chrome_frames']:>3} | {r['context'][:44]:<44} | "
                    + " ".join(r["new_tokens"][:10])
                )
        return

    if opts.command == "segments":
        segs = segment(frames)
        if opts.json:
            print(json.dumps([s.summary() for s in segs], indent=2))
        else:
            for s in segs:
                print(
                    f"[{s.index:>3}] {s.start:%H:%M:%S}-{s.end:%H:%M:%S} "
                    f"{s.duration_s/60:>6.1f}m f={len(s.frame_ids):>4} "
                    f"({s.boundary_reason[:28]:<28}) "
                    f"{list(s.contexts)[0][:36] if s.contexts else '':<36} | "
                    + " ".join(s.novel_tokens[:8])
                )
            print(f"\n{len(segs)} segments")
        return

    if opts.command == "anchors":
        found = anchors(frames, top_k=opts.top)
        if opts.json:
            print(json.dumps([a.summary() for a in found], indent=2))
        else:
            for a in found:
                print(
                    f"{a.score:>5.2f} {a.frame_id:>6} {a.timestamp:%H:%M:%S} "
                    f"{','.join(a.reasons)[:56]:<56} {a.preview[:90]}"
                )
        return

    if opts.command == "text":
        print(compact_text(frames))


if __name__ == "__main__":
    main()
