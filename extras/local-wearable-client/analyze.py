"""Basic analytics over the capture metadata sidecar (events.jsonl).

Reads the per-day `events.jsonl` files written by screen_capture.py and reports
where your time went — by app, window title, or URL — attributing the gap
between consecutive ticks to whatever was focused, and bucketing inactivity
(idle_seconds >= threshold) as "(idle)".

Pure stdlib — runs anywhere, including on a copy of the data off the Mac.

Examples:
    uv run python analyze.py                       # today, by app
    uv run python analyze.py --days 7              # last 7 days
    uv run python analyze.py --date 2026-06-17     # a specific day
    uv run python analyze.py --by title --top 30   # by window title
    uv run python analyze.py --date all            # everything on disk
"""

import argparse
import datetime as dt
import json
import os
from collections import defaultdict
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("CAPTURE_DIR", Path.home() / "ChronicleCaptures"))
IDLE_BUCKET = "(idle)"


def _day_dirs(base: Path, date_arg: str, days: int) -> list:
    """Resolve which <date> subdirectories to read."""
    if date_arg == "all":
        return sorted(p for p in base.iterdir() if p.is_dir() and _is_date(p.name))
    if date_arg and date_arg != "today":
        return [base / date_arg]
    if days > 1:
        today = dt.date.today()
        return [base / (today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    return [base / dt.date.today().isoformat()]


def _is_date(name: str) -> bool:
    try:
        dt.date.fromisoformat(name)
        return True
    except ValueError:
        return False


def _load_events(day_dirs: list) -> list:
    events = []
    for d in day_dirs:
        path = d / "events.jsonl"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    events.sort(key=lambda e: e.get("epoch", 0))
    return events


def _key_for(event: dict, by: str) -> str:
    if by == "title":
        app = event.get("app") or "?"
        title = event.get("window_title") or "(no title)"
        return f"{app} — {title}"
    if by == "url":
        return event.get("url") or "(no url)"
    return event.get("app") or "(unknown)"


def _fmt(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def aggregate(events: list, by: str, idle_threshold: float, max_gap: float) -> dict:
    totals: dict = defaultdict(float)
    for cur, nxt in zip(events, events[1:]):
        gap = nxt.get("epoch", 0) - cur.get("epoch", 0)
        if gap <= 0 or gap > max_gap:
            continue  # capture was off / paused — don't invent time
        idle = cur.get("idle_seconds")
        if idle is not None and idle >= idle_threshold:
            totals[IDLE_BUCKET] += gap
        else:
            totals[_key_for(cur, by)] += gap
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture analytics (app times)")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="capture directory")
    parser.add_argument(
        "--date", default="today", help="YYYY-MM-DD | today | all (default: today)"
    )
    parser.add_argument("--days", type=int, default=1, help="last N days (from today)")
    parser.add_argument(
        "--by",
        choices=["app", "title", "url"],
        default="app",
        help="group by (default: app)",
    )
    parser.add_argument("--top", type=int, default=20, help="show top N rows")
    parser.add_argument(
        "--idle-threshold",
        type=float,
        default=120.0,
        help="idle_seconds >= this counts as (idle) (default: 120)",
    )
    parser.add_argument(
        "--max-gap",
        type=float,
        default=5.0,
        help="ignore gaps between ticks longer than this (default: 5s)",
    )
    args = parser.parse_args()

    base = Path(args.dir)
    day_dirs = _day_dirs(base, args.date, args.days)
    events = _load_events(day_dirs)

    if not events:
        print(f"No events found in {base} for {args.date} (days={args.days}).")
        return

    totals = aggregate(events, args.by, args.idle_threshold, args.max_gap)
    tracked = sum(totals.values())
    active = tracked - totals.get(IDLE_BUCKET, 0.0)

    span_start = dt.datetime.fromtimestamp(events[0]["epoch"])
    span_end = dt.datetime.fromtimestamp(events[-1]["epoch"])

    print(f"Events:   {len(events)}  ({span_start:%Y-%m-%d %H:%M} → {span_end:%H:%M})")
    print(
        f"Tracked:  {_fmt(tracked)}   Active: {_fmt(active)}   Idle: "
        f"{_fmt(totals.get(IDLE_BUCKET, 0.0))}"
    )
    print(f"By {args.by}:")
    print("-" * 60)

    rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[: args.top]
    for name, secs in rows:
        pct = (secs / tracked * 100) if tracked else 0
        label = name if len(name) <= 42 else name[:39] + "..."
        print(f"  {_fmt(secs):>9}  {pct:5.1f}%  {label}")


if __name__ == "__main__":
    main()
