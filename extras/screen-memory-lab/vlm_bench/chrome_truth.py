"""Hand-read ground truth for the chrome-harvesting experiment.

Every string below was read by a human off the rendered PNG at 1600px, and
**written down before the `chrome` prompt existed** -- the annotation pass and the
model pass are deliberately separated so the ground truth cannot drift toward
whatever the model happened to say. Report 13 §4 contains the first three
annotations verbatim; 12015 and 12087 were added in the same pass.

"Chrome" here means the navigational furniture: tab strips, sidebars, subscription
and library lists, nav bars, thread lists, nameplates, and the counters attached to
them. Not the content being consumed.

`entities` are things a memory should be able to name. `facts` are numbers or
statements attached to them. Scoring treats them separately because a model that
lists nine games but drops "580.2 hrs" has failed differently from one that
invents a game.
"""

from __future__ import annotations

FRAMES: dict[int, dict] = {
    # ---------------------------------------------------------- YouTube Shorts
    11816: {
        "at": "2026-07-25 01:51 IST",
        "what": "YouTube Shorts in Zen browser",
        "entities": [
            # subscription sidebar -- the finding of report 13
            "RATIRL",
            "Piantwo",
            "Julia Turc",
            "Tom Scott",
            "Socially Inept",
            "Abram Engle",
            "Daily Dose Of Int",
            # the video itself
            "iclikedirt",
            "grown man btw",
            # tab strip
            "MoErgo Layout Editor",
            "Rent GPUs Online",
            "Vaani AI",
            "UGREEN HDMI KVM",
            "The Myth of Modi",
        ],
        "facts": ["Shorts", "1,748", "YouTube Premium"],
    },
    # ----------------------------------------------------------- Steam library
    12015: {
        "at": "2026-07-26 02:41 IST",
        "what": "Steam Library home",
        "entities": [
            "KillerBreadMan",
            "Age of Empires IV",
            "Pathogenic",
            "Subnautica 2",
            "Risk of Rain 2",
            "Arkheron",
            "Chained Together",
            "Brotato",
            "Ravenswatch",
            "Children of Morta",
            "Spilled",
            "PICO PARK 2",
            "ClusterPuck 99",
            "Windblown",
            "Stick Fight",
            "Boomerang Fu",
            "Unrailed 2",
            "PEAK",
            "PUBG",
            "Crimson Desert",
            "EA SPORTS FC",
            "Brawlhalla",
            "Unspottable",
            "Counter-Strike 2",
            "Wild Woods",
            "Satisfactory",
            "ABZU",
            "Assemble with Care",
            "BattleBlock Theater",
        ],
        # The point of this frame: Steam renders its OWN aggregate. No focus
        # signal, no sampling, no dwell heuristic -- the number is on the screen.
        "facts": ["580.2", "19 hrs", "99", "STORE", "LIBRARY", "COMMUNITY"],
    },
    # --------------------------------------------------------------- GitHub
    12087: {
        "at": "2026-07-26 02:59 IST",
        "what": "GitHub screenpipe/screenpipe, Agents tab. Page body is EMPTY -- "
        "all information in this frame is chrome.",
        "entities": [
            "screenpipe",
            "MoErgo Layout Editor",
            "Rent GPUs Online",
            "LinkedIn",
            "UI-JEPA",
            "GUI Agents",
            "UGREEN HDMI KVM",
            "The Myth of Modi",
        ],
        "facts": [
            "71",
            "36",
            "Issues",
            "Pull requests",
            "Agents",
            "Discussions",
            "Actions",
            "Insights",
        ],
    },
    # ------------------------------------------------------- agent workspace
    12135: {
        "at": "2026-07-26 03:19 IST",
        "what": "coding agent workspace on chronicle",
        "entities": [
            "chronicle",
            "friend-lite",
            "Research modern methods for implementation",
            "Add multimodal memory documentation",
            "Investigate Gemma 4 E2B audio benchmark anomalies",
            "Find wake word lab button prototype",
            "multimodal-memory.md",
        ],
        "facts": ["main", "dev", "15m", "21h"],
    },
    # ----------------------------------------------------------- AoE gameplay
    # ocr_chars == 0. Included to test whether chrome harvesting works where
    # text indexes return literally nothing.
    11596: {
        "at": "2026-07-26 01:16 IST",
        "what": "AoE IV gameplay, zero stored OCR",
        "entities": ["oDR.Auzio", "KillerBreadMan"],
        "facts": ["Age III", "54/80", "161", "639", "31", "14:45"],
    },
}


def norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def contains(haystack: str, needle: str) -> bool:
    """Substring match on alphanumerics only.

    Deliberately lenient about punctuation and case -- report 05's `xRaptoR72`
    lesson is that exact-case matching on gamertags produces false misses -- but
    NOT lenient about spelling, because report 08's scorer once tolerated a
    doubled letter and thereby masked the exact error it existed to catch.
    """
    return norm(needle) in norm(haystack)
