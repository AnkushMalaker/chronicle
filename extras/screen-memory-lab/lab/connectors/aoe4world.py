"""AoE4World connector -- tier 2.

Report 07 established that the question driving this whole project ("which games
did I win and lose") is a *connector* question wearing a screen-memory costume.
This module is that connector, promoted out of the hand-pasted table in
`lab/groundtruth.py`.

What it is for, in order of importance:

1. **Ground truth that labels tier 1 for free.** The API says exactly when each
   match started and how long it ran, so a frame either falls inside a real match
   or it does not -- no hand labelling, no LLM judge. See `retrieve_eval.py`.
2. **Boundaries for stage 2.** The localise stage needs to know where a match
   *ends* in order to re-read the result screen. That timestamp is a JSON key.
3. **A checkable coverage instrument.** The API says a match happened at 19:12;
   did we record anything? That is a far better capture watchdog than frame age.

No API key is required for public matches.

    GET /api/v0/players/search?query=<gamertag>   -> profile_id
    GET /api/v0/players/<profile_id>/games?since=<iso8601>

Responses are cached under `out/connectors/` so that re-scoring a pipeline does
not re-hit the network and so that a run is reproducible after the fact. Pass
`--refresh` to bypass the cache.

CLI:
    uv run python -m lab.connectors.aoe4world --since 2026-07-22 --table
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / "out" / "connectors"
BASE = "https://aoe4world.com/api/v0"
UA = "chronicle-screen-memory-lab/1.0 (research; contact via repo)"

# The user this lab is about. Kept here rather than passed everywhere, because
# every scoring script needs the same one and a typo would silently change the
# ground truth.
DEFAULT_GAMERTAG = "KillerBreadMan"


@dataclass(frozen=True)
class Match:
    """One 1v1 match, from the game server's own record.

    `result`, `opponent` and `civ` are from the *user's* perspective -- the API
    reports both sides and this class resolves which one is the user. That
    resolution is the thing screen pixels could never do reliably (report 05's
    three wrong opponent names), and here it is a dictionary lookup.
    """

    game_id: int
    started_at: str  # ISO8601 UTC
    duration_s: int
    map_name: str
    kind: str
    result: str  # win | loss
    opponent: str
    opponent_civ: str
    civ: str

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))

    @property
    def end_dt(self) -> datetime:
        return self.start_dt + timedelta(seconds=self.duration_s)

    @property
    def label(self) -> str:
        return f"{self.map_name} {self.result}"

    def __str__(self) -> str:
        return (
            f"{self.start_dt:%Y-%m-%d %H:%M} UTC  {self.result:<4}  "
            f"{self.map_name:<20} vs {self.opponent:<20} {self.duration_s:>5}s"
        )


def _get(url: str, cache_key: str, refresh: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{cache_key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    path.write_text(json.dumps(data, indent=1))
    return data


def resolve_profile_id(gamertag: str, refresh: bool = False) -> int:
    """Gamertag -> profile_id. Exact (case-insensitive) match only.

    The API's search is fuzzy and will happily return a similarly-named player.
    Report 05 got burned three times by near-miss gamertags read off pixels, so
    this refuses to guess: an inexact match raises rather than picking the top
    hit.
    """
    q = urllib.parse.quote(gamertag)
    data = _get(f"{BASE}/players/search?query={q}", f"search_{gamertag}", refresh)
    for p in data.get("players", []):
        if p.get("name", "").lower() == gamertag.lower():
            return int(p["profile_id"])
    found = [p.get("name") for p in data.get("players", [])][:5]
    raise LookupError(f"no exact match for {gamertag!r}; API returned {found}")


def fetch_matches(
    since: str,
    gamertag: str = DEFAULT_GAMERTAG,
    profile_id: int | None = None,
    refresh: bool = False,
) -> list[Match]:
    """All 1v1 matches since `since` (ISO8601 or YYYY-MM-DD), oldest first.

    Paginates. The single most expensive mistake in this project was treating one
    page of a paginated view as complete (report 07 §6a: the in-game Match
    History panel showed 10 of 18), so this follows pages until exhausted rather
    than trusting the first response.
    """
    if profile_id is None:
        profile_id = resolve_profile_id(gamertag, refresh)
    if len(since) == 10:  # YYYY-MM-DD
        since = f"{since}T00:00:00Z"

    out: list[Match] = []
    page = 1
    while True:
        url = f"{BASE}/players/{profile_id}/games?since={since}&page={page}"
        key = f"games_{profile_id}_{since.replace(':', '')}_{page}"
        data = _get(url, key, refresh)
        games = data.get("games", [])
        if not games:
            break
        for g in games:
            m = _to_match(g, profile_id)
            if m is not None:
                out.append(m)
        # total_count is the count of games, per_page the page size.
        if page * int(data.get("per_page") or 50) >= int(data.get("total_count") or 0):
            break
        page += 1

    out.sort(key=lambda m: m.started_at)
    return out


def _to_match(g: dict, profile_id: int) -> Match | None:
    """Flatten one API game into a user-perspective Match, or None if unusable."""
    me = them = None
    for team in g.get("teams", []):
        for slot in team:
            p = slot.get("player", {})
            if int(p.get("profile_id", -1)) == profile_id:
                me = p
            else:
                them = p
    if me is None or me.get("result") not in ("win", "loss"):
        return None  # ongoing, or a team game shape we do not handle
    them = them or {}
    return Match(
        game_id=int(g["game_id"]),
        started_at=g["started_at"],
        duration_s=int(g.get("duration") or 0),
        map_name=g.get("map") or "?",
        kind=g.get("kind") or "?",
        result=me["result"],
        opponent=them.get("name") or "?",
        opponent_civ=them.get("civilization") or "?",
        civ=me.get("civilization") or "?",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2026-07-22")
    ap.add_argument("--gamertag", default=DEFAULT_GAMERTAG)
    ap.add_argument("--refresh", action="store_true", help="bypass the disk cache")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--table", action="store_true")
    args = ap.parse_args()

    matches = fetch_matches(args.since, args.gamertag, refresh=args.refresh)
    if args.json:
        print(json.dumps([asdict(m) for m in matches], indent=1))
        return

    wins = sum(1 for m in matches if m.result == "win")
    print(
        f"{len(matches)} matches since {args.since}  ({wins}W-{len(matches)-wins}L)\n"
    )
    for i, m in enumerate(matches, 1):
        print(f"{i:>3}. {m}")


if __name__ == "__main__":
    main()
