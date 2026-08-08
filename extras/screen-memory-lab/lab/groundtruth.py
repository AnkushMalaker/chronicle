"""Hand-verified ground truth for the 2026-07-24 capture day.

Every field below was checked against the local ScreenPipe archive: OCR text was
read from `frames.full_text` and the decisive screens were extracted to PNG and
looked at directly. `evidence` lists the frame ids that carry the proof, so any
disagreement can be re-checked with:

    uv run python -m lab.survey frames <id> --image

Times are UTC because that is what the archive stores. The user's timezone is
Asia/Kolkata (+05:30), so this capture day spans the evening of 2026-07-24 and
the early hours of 2026-07-25 in local time -- which is exactly the midnight
boundary problem the design doc calls out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DAY_START = "2026-07-24T14:00:00+00:00"
DAY_END = "2026-07-25T01:00:00+00:00"


@dataclass
class TruthEvent:
    key: str
    event_type: str
    start: str
    end: str
    title: str
    attributes: dict
    evidence: list[int]
    tier: str  # must | should | nice
    notes: str = ""
    aliases: list[str] = field(default_factory=list)


# --------------------------------------------------------------- game events

# AoE4's own Match History panel for KillerBreadMan, screenshotted by the user on
# 2026-07-26 and reconciled against the archive by searching for each map name.
# This is the only external ground truth this project has -- everything else is a
# reading of the capture.
#
# Reverse-chronological in the panel; listed oldest-first here. `captured` is
# whether the match appears in the archive as gameplay at all.
# Source: AoE4World community API, pulled 2026-07-26.
#   GET /api/v0/players/search?query=KillerBreadMan  -> profile_id 18165770
#   GET /api/v0/players/18165770/games?since=2026-07-23T00:00:00Z
# No API key needed. This supersedes the earlier 10-row version built from a
# screenshot of the in-game Match History panel -- that panel is PAGINATED and I
# treated one page of a UI as ground truth. It was missing 8 matches.
#
# started_at is UTC as returned by the API. `captured` = present in the ScreenPipe
# archive as gameplay.
AUTHORITATIVE_RECORD = [
    # started_at (UTC),     map,                  result,    opponent,           dur_s, captured
    ("2026-07-23T03:58:36", "Enlightened Horizon", "defeat", "bodex", 190, False),
    ("2026-07-23T04:08:03", "Golden Pit", "defeat", "AfterCanvas779", 1052, False),
    ("2026-07-23T07:09:45", "West Lake", "defeat", "tony", 678, False),
    ("2026-07-23T08:45:01", "Altai", "defeat", "骑士精神", 692, False),
    ("2026-07-23T08:58:22", "Canal", "victory", "Slader935", 888, False),
    ("2026-07-23T09:21:51", "Michi", "victory", "Zeref Dragneel", 2638, False),
    ("2026-07-23T10:07:48", "Relic River", "victory", "PicklePriest295", 1230, False),
    ("2026-07-23T10:55:00", "Rocky River", "defeat", "云仙白神", 1660, False),
    ("2026-07-23T12:06:08", "Golden Pit", "victory", "kevin20020802", 1591, False),
    ("2026-07-23T12:34:01", "Lipany", "victory", "jèan paul", 1783, False),
    ("2026-07-23T13:08:07", "Black Forest", "defeat", "DeadlySham", 1259, False),
    ("2026-07-24T15:01:20", "Marshland", "victory", "WLD6116", 718, True),  # m1
    (
        "2026-07-24T15:27:50",
        "Mountain Clearing",
        "defeat",
        "xRaptoR72",
        119,
        True,
    ),  # m2
    ("2026-07-24T21:42:16", "Golden Pit", "defeat", "Ibar", 1040, True),  # m3
    ("2026-07-24T22:01:51", "Himeyama", "victory", "King Maximilian", 1539, True),  # m4
    ("2026-07-25T16:39:40", "MegaRandom", "victory", "Wyzvok [FR]", 1747, True),  # m5
    # The last two started after the most recent frame export; `captured` unknown,
    # recorded False so they are never counted as a win for extraction.
    ("2026-07-25T19:12:43", "Socotra", "defeat", "Caldas17", 718, False),
    ("2026-07-25T19:30:45", "Waterlanes", "defeat", "oDR.Auzio", 1042, False),
]

# 18 matches, 8W-10L. Only 5 are in the archive.
#
# The uncaptured ones are NOT an extraction failure. Eleven fall inside a
# 34.9-hour capture outage (2026-07-23 09:33 -> 2026-07-24 20:28 IST); the first
# 07-23 match ended right at its leading edge. Across the archive's 90-hour span,
# 55 hours (61%) have no capture at all.
#
# End-to-end recall for the window actually analysed is 5/16 = 31%.
#
# Consequence for anyone scoring against GAMES below: those scores measure
# extraction *given a recording*, which is the right thing to measure. They are
# not end-to-end recall of what the user actually did. Do not quote them as such.
#
# `xRaptoR72` above is the API's spelling -- independent, non-generative
# confirmation of the m2 opponent that two gemma4 models got wrong (`xRaptorR72`)
# and that I wrongly treated their agreement as corroborating.
#
# See docs/research/screen-memory/07-how-real-systems-do-it.md: every field this
# lab extracts from pixels is a key in this API's JSON, which is why the AoE4
# question was the wrong test case for screen memory.

GAMES = [
    TruthEvent(
        key="m1",
        event_type="game_match_completed",
        start="2026-07-24T15:00:17+00:00",
        end="2026-07-24T15:13:22+00:00",
        title="Won an Age of Empires IV 1v1 against WLD6116 on Marshland",
        attributes={
            "game": "Age of Empires IV",
            "mode": "Quick Match 1v1 Standard",
            "map": "Marshland",
            "opponent": "WLD6116",
            "outcome": "victory",
            "how_it_ended": "opponent WLD6116 surrendered, then was eliminated",
            "duration": "11:40",
            "player": "KillerBreadMan",
        },
        evidence=[6875, 6877, 6880, 7063, 7064, 7066],
        tier="must",
        notes=(
            "Result screen present and OCR'd. Easiest of the four. Frame 7063 shows "
            "'WLD6116 surrendered' before the elimination line, so the victory came "
            "from the opponent conceding."
        ),
    ),
    TruthEvent(
        key="m2",
        event_type="game_match_completed",
        start="2026-07-24T15:27:12+00:00",
        end="2026-07-24T15:29:49+00:00",
        title="Surrendered an Age of Empires IV 1v1 on Mountain Clearing about a minute in",
        attributes={
            "game": "Age of Empires IV",
            "mode": "Quick Match 1v1 Standard",
            "map": "Mountain Clearing",
            "opponent": "xRaptoR72",
            "outcome": "defeat_by_surrender",
            "duration": "~00:50",
            "reason": "disliked the map; said it should have been excluded",
            "player": "KillerBreadMan",
        },
        evidence=[7157, 7159, 7160, 7162, 7170, 7171, 7172, 7173],
        tier="must",
        notes=(
            "Corrected three times. The opponent name is a cautionary tale.\n\n"
            "1. Originally recorded as unknown. A prototype extracted 'XRaptoR72' and "
            "re-checking frame 7159 confirmed a name was there. ScreenPipe's OCR also "
            "stores 'XRaptoR72'. Then gemma-4-E2B and gemma-4-12B *both* read "
            "'xRaptorR72', and I changed it to that, reasoning that two models "
            "agreeing beat one human transcription. That was wrong: the user says "
            "'xRaptoR72' and reading the pixels at 1900px confirms it. The models "
            "agreed on the same WRONG spelling -- they inserted a doubled letter. "
            "Agreement between models is not independent evidence; they share failure "
            "modes. OCR got the letters right and the case wrong (X for x); the models "
            "got the case right and added a letter. Note also that score_vlm.py's "
            "entity matcher squeezes doubled letters, so it scored the models' wrong "
            "spelling as a hit -- a scoring tolerance that hid the very error it "
            "should have surfaced.\n\n"
            "2. This entry used to say 'no result screen at all', and claimed the only "
            "proof was chat text -- 'hate this map. i shouldve exluded it . my bad' / "
            "'ill surrender to save time' (7171) and 'KillerBreadMan surrendered' "
            "(7172). That was wrong. Frame 7173 (15:29:49Z) IS a full-screen DEFEAT "
            "banner, verified by eye. It was missed because **ScreenPipe's OCR stored "
            "zero characters for it** -- the word is large stylised serif on a textured "
            "cloth banner, which the OCR engine returns nothing for. So the frame is "
            "also unrankable by typographic salience (no OCR means no `elements` rows). "
            "gemma-4-12B found it on a blind 1-frame-per-600s clock grid.\n\n"
            "The lesson this case actually teaches is therefore the opposite of what it "
            "was recorded as teaching: not 'result screens are not enough' but 'the "
            "stored text is not enough, and absence of OCR text is not absence of "
            "information'."
        ),
    ),
    TruthEvent(
        key="m3",
        event_type="game_match_completed",
        start="2026-07-24T21:40:58+00:00",
        end="2026-07-24T22:00:56+00:00",
        title="Lost an Age of Empires IV 1v1 against Ibar on Golden Pit",
        attributes={
            "game": "Age of Empires IV",
            "mode": "Quick Match 1v1 Standard",
            "map": "Golden Pit",
            "opponent": "Ibar",
            "outcome": "defeat",
            "duration": "17:21",
            "player": "KillerBreadMan",
            "chat": "asked the opponent what counters cheap archers and men-at-arms",
        },
        evidence=[8303, 8306, 8309, 8572, 8577, 8583],
        tier="must",
        notes="Result screen present. Opponent named on both the lobby and result screens.",
    ),
    TruthEvent(
        key="m4",
        event_type="game_match_completed",
        start="2026-07-24T22:01:44+00:00",
        end="2026-07-24T22:27:41+00:00",
        title="Won an Age of Empires IV 1v1 against King Maximilian on Himeyama",
        attributes={
            "game": "Age of Empires IV",
            "mode": "Quick Match 1v1 Standard",
            "map": "Himeyama",
            "opponent": "King Maximilian",
            "outcome": "victory",
            "duration": "25:51",
            "player": "KillerBreadMan",
        },
        evidence=[8588, 8591, 8975, 8981],
        tier="must",
        notes=(
            "Starts 48 seconds after the previous match's result screen was still on "
            "screen, so a pipeline keyed on application context will merge m3 and m4."
        ),
    ),
]

# ---------------------------------------------------------- non-game events
# These exist so a prototype cannot score well by hard-coding game vocabulary.

OTHER = [
    TruthEvent(
        key="session_record",
        event_type="game_session_rollup",
        start="2026-07-24T15:00:17+00:00",
        end="2026-07-24T22:27:41+00:00",
        title="Age of Empires IV session went 2-2 across four 1v1 matches",
        attributes={
            "wins": 2,
            "losses": 2,
            "ladder_record_before_session": "97 wins / 93 losses solo",
        },
        evidence=[6876],
        tier="should",
        notes="Ladder record is visible once, on the multiplayer menu at 15:00:45.",
    ),
    TruthEvent(
        key="screenpipe_fix",
        event_type="software_fix_completed",
        start="2026-07-24T15:30:53+00:00",
        end="2026-07-24T15:42:30+00:00",
        title="Worked on making the custom ScreenPipe AppImage survive updates",
        attributes={
            "project": "screenpipe",
            "topics": [
                "desktop entry",
                "autostart",
                "AppImage persistence",
                "bind mount",
            ],
            "tooling": "Codex CLI (GPT-5.6-Sol, full access)",
        },
        evidence=[7223, 7229, 7232, 7235, 7238, 7244],
        tier="should",
        notes=(
            "The resolution half of the problem the previous audit found half-retained. "
            "Also a Rust build of screenpipe-audio/screenpipe-capture is visible (7176)."
        ),
    ),
    TruthEvent(
        key="appimage_build_failure",
        event_type="build_failed",
        start="2026-07-24T15:48:56+00:00",
        end="2026-07-24T15:57:40+00:00",
        title="A Tauri AppImage build failed because bun was not on PATH",
        attributes={
            "project": "chronicle",
            "command": "tauri build --bundles appimage",
            "cause": 'beforeBuildCommand "bun run build" -> bun: command not found',
            "outcome": "build did not complete",
        },
        evidence=[7374, 7375, 7376],
        tier="should",
        notes=(
            "Added after two pipelines reported it independently and the OCR was "
            "re-checked by hand: frame 7375 carries both 'beforeBuildCommand \"bun run "
            "build\"' and 'bun: command not found'. A clean example of an event whose "
            "whole substance is one error string in a terminal."
        ),
    ),
    TruthEvent(
        key="inverter_research",
        event_type="purchase_research",
        start="2026-07-24T15:33:40+00:00",
        end="2026-07-24T15:35:00+00:00",
        title="Searched for inverter setup options in Bangalore",
        attributes={
            "query": "inverter setup bangalore",
            "location": "Bengaluru, Karnataka",
        },
        evidence=[7190, 7193, 7196, 7199],
        tier="should",
        notes="Short, text-only, no outcome. Tests whether low-signal research is kept or dropped.",
    ),
    TruthEvent(
        key="fluidvoice",
        event_type="tool_discovery",
        start="2026-07-24T15:41:00+00:00",
        end="2026-07-24T16:11:00+00:00",
        title="Looked at FluidVoice, an open-source voice-to-text tool",
        attributes={"subject": "FluidVoice", "kind": "open source dictation app"},
        evidence=[7277],
        tier="nice",
    ),
    TruthEvent(
        key="skin_lumps_video",
        event_type="media_watched",
        start="2026-07-24T16:28:50+00:00",
        end="2026-07-24T16:43:54+00:00",
        title="Watched a YouTube video about common skin lumps and bumps",
        attributes={
            "platform": "YouTube",
            "title": "10 Common Skin Lumps and Bumps You Should Know About",
            "creator": "Dr. Danny Guo",
        },
        evidence=[],
        tier="nice",
        notes="Window title carries this; possibly health-sensitive, so a retention decision.",
    ),
    TruthEvent(
        key="audit_session",
        event_type="analysis_session",
        start="2026-07-24T22:35:00+00:00",
        end="2026-07-25T00:45:00+00:00",
        title="Ran an agent audit of the ScreenPipe observation pipeline and wrote up multimodal memory",
        attributes={
            "tooling": "Claude Code",
            "repo": "chronicle",
            "artifact": "docs/multimodal-memory.md",
        },
        evidence=[9048, 9239, 9652],
        tier="should",
        notes=(
            "The attribution trap. These frames are full of the words VICTORY, Defeat, "
            "Golden Pit and Himeyama because an assistant was writing ABOUT the matches. "
            "See TRAPS below."
        ),
    ),
]

# ------------------------------------------------------------------- traps
# Assertions a correct pipeline must NOT make. Each names the frames that bait it.

TRAPS = [
    {
        "key": "trap_assistant_text_as_gameplay",
        "claim_that_is_wrong": "A game match happened between 22:35 and 00:45 UTC.",
        "why": (
            "Frames 9048-9652 are a Claude Code session discussing the earlier matches. "
            "'Victory', 'Defeat', 'Ibar', 'Golden Pit', 'Himeyama' and even a full results "
            "table appear as assistant-generated text, not as game UI."
        ),
        "bait_frames": [9048, 9100, 9239, 9402, 9652],
    },
    {
        "key": "trap_background_tab_gamertag",
        "claim_that_is_wrong": (
            "Age of Empires was being played during 2026-07-22 01:00-13:00 UTC "
            "(or at any point that day)."
        ),
        "why": (
            "'KillerBreadMan' appears in ~700 frames that day purely because a background "
            "Zen browser tab was in the accessibility text dump. text_source='accessibility' "
            "captures background tabs and browser chrome, not what was on screen."
        ),
        "bait_frames": [181, 1095, 1096, 1097],
    },
    {
        "key": "trap_result_screen_left_open",
        "claim_that_is_wrong": "The Golden Pit defeat happened at 22:00:56 (last frame showing it).",
        "why": (
            "The defeat result screen stayed on screen from 21:59:40 to 22:00:56 while the "
            "user alt-tabbed. The match ended at 21:59:25 ('KillerBreadMan has been "
            "eliminated'); later frames are the same screen persisting."
        ),
        "bait_frames": [8582, 8583],
    },
    {
        "key": "trap_biome_read_as_map",
        "claim_that_is_wrong": "The second match was played on a map called Alpine Spring.",
        "why": (
            "Frame 7170 is the pause menu, which shows 'Mountain Clearing' (the map), "
            "'ALPINE SPRING' (the biome) and a map seed together. The biome is rendered "
            "in larger type than the map name, so an extractor that takes the most "
            "prominent string reports the biome. One pipeline did exactly this."
        ),
        "bait_frames": [7170],
    },
    {
        "key": "trap_map_browser_names",
        "claim_that_is_wrong": "The match was played on Boulder Bay / African Waters / Hedgemaze / etc.",
        "why": (
            "Frames 8283-8301 are the map-ban/exclusion browser, which lists dozens of map "
            "names. Only the lobby screen states the map that was actually played."
        ),
        "bait_frames": [8283, 8287, 8294, 8298, 8300, 8301],
    },
]

ALL_EVENTS = GAMES + OTHER


def by_tier(tier: str) -> list[TruthEvent]:
    return [e for e in ALL_EVENTS if e.tier == tier]


def as_dicts() -> list[dict]:
    return [
        {
            "key": e.key,
            "event_type": e.event_type,
            "start": e.start,
            "end": e.end,
            "title": e.title,
            "attributes": e.attributes,
            "evidence": e.evidence,
            "tier": e.tier,
            "notes": e.notes,
        }
        for e in ALL_EVENTS
    ]


if __name__ == "__main__":
    import json

    print(json.dumps({"events": as_dicts(), "traps": TRAPS}, indent=2))
