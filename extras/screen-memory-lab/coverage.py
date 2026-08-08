"""Capture coverage, checked against a connector rather than guessed.

Report 07 §8 item 2: with tier-2 connectors in place, coverage becomes
*checkable*. The API says a match happened at 19:12 UTC -- did we record
anything? That is a much better watchdog than frame age, because frame age
cannot tell "the user did nothing" apart from "we recorded nothing", which is
the failure that cost this project 11 of 18 matches.

For every match the connector reports, this counts archive frames inside the
match window and classifies coverage. It also reports the *tail* separately:
a match whose gameplay was captured but whose final seconds were not is
unanswerable by stage 2, because the result banner lives there.

    uv run python coverage.py --since 2026-07-22
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import timedelta
from pathlib import Path

from lab.connectors.aoe4world import fetch_matches

DB = Path.home() / ".screenpipe" / "db.sqlite"

# The result banner lands at or just after the API's end time. Report 08 used the
# same 120s and reported it explicitly rather than burying it.
TAIL_S = 120


def frame_count(con: sqlite3.Connection, start_iso: str, end_iso: str) -> int:
    cur = con.execute(
        "SELECT COUNT(*) FROM frames WHERE timestamp >= ? AND timestamp <= ?",
        (start_iso, end_iso),
    )
    return int(cur.fetchone()[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-22")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    matches = fetch_matches(args.since)
    # Read-only, and never write to the archive.
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    rows = []
    for m in matches:
        body = frame_count(con, m.start_dt.isoformat(), m.end_dt.isoformat())
        tail = frame_count(
            con,
            m.end_dt.isoformat(),
            (m.end_dt + timedelta(seconds=TAIL_S)).isoformat(),
        )
        if body == 0:
            state = "MISSING"
        elif tail == 0:
            state = "no-tail"  # gameplay seen, result screen not
        else:
            state = "ok"
        rows.append((m, body, tail, state))

    print(
        f"{'started (UTC)':<17} {'result':<5} {'map':<20} {'body':>5} {'tail':>5}  state"
    )
    print("-" * 74)
    for m, body, tail, state in rows:
        print(
            f"{m.start_dt:%Y-%m-%d %H:%M}   {m.result:<5} {m.map_name:<20} "
            f"{body:>5} {tail:>5}  {state}"
        )

    n = len(rows)
    ok = sum(1 for *_, s in rows if s == "ok")
    notail = sum(1 for *_, s in rows if s == "no-tail")
    missing = sum(1 for *_, s in rows if s == "MISSING")
    print("-" * 74)
    print(
        f"{n} matches: {ok} answerable, {notail} gameplay-only (no result screen), {missing} never recorded"
    )
    print(f"Ceiling on any extraction pipeline: {ok}/{n} = {ok/n*100:.0f}%")


if __name__ == "__main__":
    main()
