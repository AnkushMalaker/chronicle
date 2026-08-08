"""Can "how many times did I visit site X" be answered from this archive?

This is a deliberately different question from the AoE4 one. The match question
is about *detecting a rare salient event*. This one is about *counting a
recurring mundane one*. If a screen-memory design only handles the first, it
answers "did I win" and cannot answer "am I spending too long on YouTube".

Three candidate signals are tested, cheapest first:

* ``frames.browser_url`` -- the field built for exactly this. Measured empty on
  every frame in this archive, so it is reported and skipped.
* ``frames.window_name`` -- "<page title> — Zen Browser". Present and clean.
* ``frames.accessibility_text`` -- contains URLs, but also contains the entire
  accessibility tree including context menus that were never on screen, so any
  URL found there may be a link on the page rather than the page visited.

A "visit" is a contiguous run of frames on the same page, allowing a short gap
for capture misses. Two frames on the same page an hour apart are two visits.
This is the definition a person means by "how many times", and it is the one
thing a raw frame count cannot give you.

Run:
    uv run python site_visits.py
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from lab.spipe import open_archive

OUT = Path(__file__).resolve().parent / "out" / "verify"
VISIT_GAP = timedelta(minutes=3)

# Title suffixes that name the site. Deliberately a short list: the point is to
# show what a domain-blind rule recovers, not to hand-maintain a site registry.
TITLE_SITE = [
    (re.compile(r" - YouTube$"), "youtube.com"),
    (re.compile(r"^\(\d+\) WhatsApp$|^WhatsApp$"), "web.whatsapp.com"),
    (re.compile(r"Discord \|"), "discord.com"),
    (re.compile(r"·\s*Miruro"), "miruro.to"),
    (re.compile(r"\| Re:ANIME$"), "re-anime"),
    (re.compile(r"· (Pull Request|Issue) #\d+ ·"), "github.com"),
    (re.compile(r"^GitHub -|/[\w.-]+: "), "github.com"),
    (re.compile(r"Claude$|^Claude"), "claude.ai"),
    (re.compile(r"ChatGPT"), "chatgpt.com"),
    (re.compile(r"Gmail|Inbox \(\d+\)"), "mail.google.com"),
]

URL_RE = re.compile(r"https?://([\w.-]+)")


def site_of(title: str) -> str | None:
    for pat, site in TITLE_SITE:
        if pat.search(title):
            return site
    return None


def main() -> None:
    arc = open_archive()
    lo, hi = arc.span()
    frames = arc.frames(lo.isoformat(), (hi + timedelta(seconds=1)).isoformat())
    OUT.mkdir(parents=True, exist_ok=True)

    browser = [f for f in frames if (f.window_name or "").endswith("Zen Browser")]
    print(f"{len(frames)} frames total, {len(browser)} browser frames")

    # ------------------------------------------------- signal 1: browser_url
    with_url = [f for f in frames if f.browser_url]
    print(
        f"\nsignal 1  frames.browser_url : populated on {len(with_url)}/{len(frames)} "
        f"frames -- {'UNUSABLE' if not with_url else 'usable'}"
    )

    # ---------------------------------------------- signal 3: a11y-tree URLs
    # Checked before titles so the title result is not read as the only option.
    a11y_urls = Counter()
    a11y_frames = 0
    for f in browser:
        hosts = set(URL_RE.findall(f.accessibility_text or ""))
        if hosts:
            a11y_frames += 1
            a11y_urls.update(hosts)
    print(
        f"signal 3  accessibility_text URLs : {a11y_frames}/{len(browser)} browser "
        f"frames carry >=1 URL, {len(a11y_urls)} distinct hosts"
    )
    print(f"          most common hosts: {a11y_urls.most_common(8)}")
    multi = sum(
        1 for f in browser if len(set(URL_RE.findall(f.accessibility_text or ""))) > 1
    )
    print(
        f"          frames carrying MORE THAN ONE host: {multi} "
        f"({100*multi/max(1,a11y_frames):.0f}% of URL-carrying frames) -- "
        f"so a URL here does not identify the page being viewed"
    )

    # ------------------------------------------------ signal 2: window_name
    titled = []
    for f in browser:
        m = re.match(r"^(.*?)\s+[—-]\s+Zen Browser$", f.window_name)
        if m and m.group(1).strip():
            titled.append((f, m.group(1).strip()))
    print(
        f"signal 2  window_name titles : parsed on {len(titled)}/{len(browser)} "
        f"browser frames ({100*len(titled)/max(1,len(browser)):.1f}%)"
    )

    # ----------------------------------------------------------- visit runs
    visits_by_title: dict[str, list] = defaultdict(list)
    prev_title, run_start, run_last = None, None, None
    for f, title in titled:
        if title != prev_title or (run_last and f.timestamp - run_last > VISIT_GAP):
            if prev_title is not None:
                visits_by_title[prev_title].append((run_start, run_last))
            prev_title, run_start = title, f.timestamp
        run_last = f.timestamp
    if prev_title is not None:
        visits_by_title[prev_title].append((run_start, run_last))

    per_site = defaultdict(lambda: {"visits": 0, "seconds": 0.0, "titles": set()})
    unmatched = Counter()
    for title, runs in visits_by_title.items():
        site = site_of(title)
        if site is None:
            unmatched[title] += len(runs)
            site = "(unclassified)"
        per_site[site]["visits"] += len(runs)
        per_site[site]["seconds"] += sum((b - a).total_seconds() for a, b in runs)
        per_site[site]["titles"].add(title)

    print(
        f"\n--- visits per site ({VISIT_GAP.total_seconds()/60:.0f}-minute gap = new visit) ---"
    )
    print(f"{'site':<22} {'visits':>7} {'dwell':>10} {'pages':>6}")
    rows = sorted(per_site.items(), key=lambda kv: -kv[1]["visits"])
    for site, d in rows:
        h, rem = divmod(int(d["seconds"]), 3600)
        print(
            f"{site:<22} {d['visits']:>7} {h:>4}h{rem//60:02d}m "
            f"{len(d['titles']):>6}"
        )

    print(f"\n--- distinct pages visited most often ---")
    top = sorted(visits_by_title.items(), key=lambda kv: -len(kv[1]))[:12]
    for title, runs in top:
        secs = sum((b - a).total_seconds() for a, b in runs)
        print(f"  {len(runs):>3} visits  {int(secs)//60:>4}m  {title[:66]}")

    print(
        f"\n{sum(unmatched.values())} visits across {len(unmatched)} titles were "
        f"unclassifiable by the {len(TITLE_SITE)} suffix rules; top examples:"
    )
    for title, n in unmatched.most_common(6):
        print(f"  {n:>3}  {title[:70]}")

    payload = {
        "frames_total": len(frames),
        "browser_frames": len(browser),
        "browser_url_populated": len(with_url),
        "titles_parsed": len(titled),
        "a11y_url_frames": a11y_frames,
        "a11y_multi_host_frames": multi,
        "visit_gap_minutes": VISIT_GAP.total_seconds() / 60,
        "per_site": {
            s: {
                "visits": d["visits"],
                "dwell_seconds": round(d["seconds"]),
                "distinct_pages": len(d["titles"]),
            }
            for s, d in rows
        },
        "unclassified_visits": sum(unmatched.values()),
    }
    (OUT / "site_visits.json").write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {OUT}/site_visits.json")


if __name__ == "__main__":
    main()
