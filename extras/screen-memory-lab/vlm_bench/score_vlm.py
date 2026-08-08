"""Score gemma4 scene-description output against per-frame expectations.

Scoring is deterministic wherever it can be. Every expectation below is a
property a human verified about that specific frame, so there is no LLM judge in
the loop and no room for a judge to be talked into a wrong verdict. Where a
check cannot be made mechanically (the free-form `describe` prompt) the output
is only summarised, not scored.

Four things are measured:

**announcement**  Does `is_state_announcement` fire on the result screens and
stay quiet on menus and gameplay? This is the direct comparison against
typographic salience, which is the heuristic the VLM would replace.

**entities**  Does it read the names that make an event useful -- opponent, map?
An event record saying "you lost a match" is worth much less than one saying
"you lost to Ibar on Golden Pit".

**traps**  Three frames show an assistant *writing about* the matches. A frame
that describes an event is not the event. Reporting these as game outcomes is
the single most expensive failure available on this day, because it silently
doubles the match count.

**loop**  For triage, what did the model ask for, and did supplying it change
the answer? A model that always asks for more, or never does, cannot drive a
variable capture rate.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# What a correct reading of each frame looks like. `announcement` is the
# expected value of is_state_announcement; None means genuinely ambiguous and is
# excluded from that score rather than scored generously.
EXPECT = {
    6871: {"announcement": False, "kind": "menu", "entities": []},
    6900: {"announcement": False, "kind": "gameplay", "entities": []},
    7050: {"announcement": False, "kind": "gameplay", "entities": []},
    7130: {"announcement": None, "kind": "aftermath", "entities": []},
    7152: {
        "announcement": False,
        "kind": "menu",
        "entities": [],
        "trap": "map_exclusion_is_not_a_choice",
    },
    7158: {"announcement": False, "kind": "menu", "entities": []},
    # The gamertag here has been wrong three times. OCR stores "XRaptoR72"
    # (right letters, wrong case); E2B and the 12B both produced "xRaptorR72"
    # (right case, one letter too many); the truth, confirmed by the user and by
    # reading the frame at 1900px, is "xRaptoR72". Two models agreeing was NOT
    # corroboration -- same family, same failure mode. See entity_present() for
    # the tolerance that let the models' version score as a hit anyway.
    7159: {
        "announcement": False,
        "kind": "lobby",
        "entities": ["xRaptoR72", "KillerBreadMan"],
    },
    7160: {"announcement": False, "kind": "menu", "entities": []},
    8280: {"announcement": False, "kind": "menu", "entities": []},
    8294: {
        "announcement": False,
        "kind": "menu",
        "entities": [],
        "trap": "alpine_spring_is_a_biome",
    },
    8302: {"announcement": False, "kind": "menu", "entities": []},
    8577: {
        "announcement": True,
        "kind": "result",
        "entities": ["Ibar", "Golden Pit"],
        "outcome": "defeat",
    },
    8581: {
        "announcement": True,
        "kind": "result",
        "entities": ["Ibar", "Golden Pit"],
        "outcome": "defeat",
    },
    8981: {
        "announcement": True,
        "kind": "result",
        "entities": ["Maximilian", "Himeyama"],
        "outcome": "victory",
    },
    9048: {
        "announcement": None,
        "kind": "assistant_text",
        "entities": [],
        "trap": "text_about_matches",
    },
    9200: {
        "announcement": None,
        "kind": "assistant_text",
        "entities": [],
        "trap": "text_about_matches",
    },
    9652: {
        "announcement": None,
        "kind": "assistant_text",
        "entities": [],
        "trap": "text_about_matches",
    },
}

TRAP_FRAMES = {f for f, e in EXPECT.items() if e.get("trap") == "text_about_matches"}
RESULT_FRAMES = {f for f, e in EXPECT.items() if e.get("kind") == "result"}


def blob(parsed) -> str:
    return json.dumps(parsed, ensure_ascii=False).lower() if parsed else ""


def entity_present(expected: str, text: str) -> bool:
    """Is `expected` in `text`, tolerating the spelling drift VLMs produce?

    Both models rendered real names slightly wrong -- "King Maximillian" for
    Maximilian, "KillerFreddMan" for KillerBreadMan. Scoring those as misses
    measures my string equality, not the model's reading, so matching is done on
    a squeezed form (lowercase, no doubled letters, alphanumerics only). The
    looseness is disclosed rather than hidden: it would accept a genuinely wrong
    name that happened to squeeze to the same string, which is a real if small
    risk on names this distinctive.
    """

    def squeeze(s: str) -> str:
        s = re.sub(r"[^a-z0-9]", "", s.lower())
        return re.sub(r"(.)\1+", r"\1", s)

    return squeeze(expected) in squeeze(text)


def announcement_of(parsed) -> object:
    """Read is_state_announcement, tolerating the model misspelling the key.

    gemma-4-12B returned "is_state_annoncement" (missing a u) on 8 of 17 frames.
    That is a real schema-compliance defect and is reported as such, but it must
    not be scored as a perception failure -- the value it produced was usually
    right.
    """
    if not parsed:
        return None
    for k in parsed:
        if re.sub(r"[^a-z]", "", k.lower()).startswith("isstateann"):
            return parsed[k]
    return None


def claims_game_outcome(parsed) -> bool:
    """Does this record assert a match result happened on this screen?"""
    if not parsed:
        return False
    if parsed.get("event") is True:
        text = blob(parsed)
        return any(
            w in text for w in ("victor", "defeat", "won", "lost", "win", "loss")
        )
    if announcement_of(parsed) is True:
        text = f"{parsed.get('activity','')} {parsed.get('salient_text','')}".lower()
        return any(w in text for w in ("victor", "defeat", "won", "lost"))
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()

    for path in args.files:
        recs = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
        if not recs:
            print(f"\n=== {path}: empty ===")
            continue
        model = recs[0]["model"]
        print(f"\n{'='*72}\n{path}   model={model}   records={len(recs)}\n{'='*72}")

        # ------------------------------------------------------- throughput
        r1 = [r for r in recs if r.get("round", 1) == 1 and "seconds" in r]
        if r1:
            secs = sorted(r["seconds"] for r in r1)
            ins = [r["input_tokens"] for r in r1]
            outs = [r["output_tokens"] for r in r1]
            print(
                f"latency  median {secs[len(secs)//2]:.2f}s  p90 {secs[int(len(secs)*0.9)]:.2f}s  "
                f"total {sum(secs)/60:.1f}min for {len(r1)} calls"
            )
            print(
                f"tokens   in median {sorted(ins)[len(ins)//2]}  "
                f"out median {sorted(outs)[len(outs)//2]}"
            )
        errs = [r for r in recs if r.get("error")]
        bad = [r for r in recs if r.get("parse_error")]
        if errs:
            print(
                f"errors   {len(errs)}: {Counter(r['error'][:60] for r in errs).most_common(3)}"
            )
        print(
            f"json     parse failures {len(bad)}/{len([r for r in recs if 'parsed' in r])}"
        )

        targeted = any(r.get("frame_id") in EXPECT for r in recs)
        if not targeted:
            # Grid run: no per-frame truth, so report distributions only.
            for name in sorted({r["prompt_name"] for r in recs}):
                sub = [r for r in recs if r["prompt_name"] == name and r.get("parsed")]
                if not sub:
                    continue
                if name == "triage":
                    keep = sum(
                        1 for r in sub if r["parsed"].get("worth_keeping") is True
                    )
                    needs = Counter(r["parsed"].get("need") for r in sub)
                    print(
                        f"\n{name}: worth_keeping {keep}/{len(sub)} "
                        f"({100*keep/len(sub):.0f}%)  needs={dict(needs.most_common())}"
                    )
                if name == "structured":
                    ann = sum(
                        1
                        for r in sub
                        if r["parsed"].get("is_state_announcement") is True
                    )
                    acts = Counter(str(r["parsed"].get("activity"))[:40] for r in sub)
                    print(
                        f"\n{name}: announcements {ann}/{len(sub)} "
                        f"({100*ann/len(sub):.0f}%)"
                    )
                    print("  top activities:")
                    for a, n in acts.most_common(8):
                        print(f"    {n:>3}  {a}")
                if name == "event":
                    ev = [r for r in sub if r["parsed"].get("event") is True]
                    print(f"\n{name}: events {len(ev)}/{len(sub)}")
                    for r in ev[:10]:
                        p = r["parsed"]
                        print(
                            f"    f{r['frame_id']} {r.get('local','')}  "
                            f"{str(p.get('kind'))[:22]:<22} {str(p.get('outcome'))[:40]}"
                        )
            continue

        # ------------------------------------------------- announcement score
        for name in ("structured",):
            sub = {
                r["frame_id"]: r
                for r in recs
                if r["prompt_name"] == name and r.get("round", 1) == 1
            }
            if not sub:
                continue
            ok = tot = 0
            wrong = []
            for fid, exp in EXPECT.items():
                if exp["announcement"] is None or fid not in sub:
                    continue
                got = announcement_of(sub[fid].get("parsed"))
                tot += 1
                if got is exp["announcement"]:
                    ok += 1
                else:
                    wrong.append((fid, exp["kind"], got))
            print(f"\nannouncement detection ({name}): {ok}/{tot} correct")
            for fid, kind, got in wrong:
                print(f"  WRONG f{fid} ({kind}): said {got}")

            # entities on the frames where names are readable
            print("entity recall:")
            for fid, exp in EXPECT.items():
                if not exp["entities"] or fid not in sub:
                    continue
                text = blob((sub[fid].get("parsed") or {}))
                hit = [e for e in exp["entities"] if entity_present(e, text)]
                print(
                    f"  f{fid} {exp['kind']:<10} {len(hit)}/{len(exp['entities'])} "
                    f"got={hit} missing={[e for e in exp['entities'] if e not in hit]}"
                )

        # --------------------------------------- outcome / concluded prompts
        # These ask directly for a result instead of asking the model to
        # classify the screen. The trap frames matter more here than anywhere
        # else: assistant text about the matches contains the literal words
        # "Victory" and "Defeat", so a prompt that keys on result vocabulary
        # can be fooled by text that merely mentions an outcome.
        for name, key in (
            ("outcome", "states_a_result"),
            ("concluded", "something_concluded"),
        ):
            sub = {
                r["frame_id"]: r
                for r in recs
                if r["prompt_name"] == name and r.get("round", 1) == 1
            }
            if not sub:
                continue
            hit = [
                f
                for f in sorted(RESULT_FRAMES & set(sub))
                if (sub[f].get("parsed") or {}).get(key) is True
            ]
            print(
                f"\n{name} prompt: result screens detected "
                f"{len(hit)}/{len(RESULT_FRAMES & set(sub))}"
            )
            for f in sorted(RESULT_FRAMES & set(sub)):
                p = sub[f].get("parsed") or {}
                print(
                    f"  f{f} {key}={p.get(key)}  "
                    f"word={str(p.get('result_word') or p.get('evidence'))[:30]}  "
                    f"outcome={str(p.get('what_finished') or p.get('outcome'))[:34]}"
                )
            fp = [
                f
                for f, exp in EXPECT.items()
                if exp["kind"] in ("menu", "gameplay", "lobby")
                and f in sub
                and (sub[f].get("parsed") or {}).get(key) is True
            ]
            print(f"  false positives on menus/gameplay: {len(fp)} {fp}")
            trap_fp = [
                f
                for f in sorted(TRAP_FRAMES & set(sub))
                if (sub[f].get("parsed") or {}).get(key) is True
            ]
            print(
                f"  TRAP violations (assistant text read as a result): "
                f"{len(trap_fp)} {trap_fp}"
            )
            for f in trap_fp:
                p = sub[f].get("parsed") or {}
                print(
                    f"    f{f} claimed {str(p.get('result_word') or p.get('evidence'))[:40]!r} "
                    f"-> {str(p.get('what_finished') or p.get('outcome'))[:40]}"
                )

        # -------------------------------------------------------- event score
        sub = {
            r["frame_id"]: r
            for r in recs
            if r["prompt_name"] == "event" and r.get("round", 1) == 1
        }
        if sub:
            found = [
                f
                for f in RESULT_FRAMES
                if f in sub and (sub[f].get("parsed") or {}).get("event") is True
            ]
            print(
                f"\nevent prompt: result screens flagged {len(found)}/{len(RESULT_FRAMES & set(sub))}"
            )
            for f in sorted(RESULT_FRAMES & set(sub)):
                p = sub[f].get("parsed") or {}
                print(
                    f"  f{f} event={p.get('event')}  outcome={str(p.get('outcome'))[:52]}  "
                    f"where={str(p.get('where'))[:24]}"
                )

        # -------------------------------------------------------------- traps
        print("\ntrap frames (assistant text about matches -- must NOT be an outcome):")
        for f in sorted(TRAP_FRAMES):
            for name in ("structured", "event"):
                r = next(
                    (
                        x
                        for x in recs
                        if x["frame_id"] == f
                        and x["prompt_name"] == name
                        and x.get("round", 1) == 1
                    ),
                    None,
                )
                if not r:
                    continue
                p = r.get("parsed") or {}
                violated = claims_game_outcome(p)
                print(
                    f"  f{f} {name:<11} {'VIOLATION' if violated else 'ok':<10} "
                    f"{str(p.get('activity') or p.get('reason') or p.get('outcome'))[:56]}"
                )

        # --------------------------------------------------------------- loop
        r2 = [r for r in recs if r.get("round") == 2]
        tri = [
            r for r in recs if r["prompt_name"] == "triage" and r.get("round", 1) == 1
        ]
        if tri:
            needs = Counter((r.get("parsed") or {}).get("need") for r in tri)
            keep = sum(
                1 for r in tri if (r.get("parsed") or {}).get("worth_keeping") is True
            )
            print(
                f"\ntriage round 1: worth_keeping {keep}/{len(tri)}; "
                f"needs={dict(needs.most_common())}"
            )
        if r2:
            flips = [r for r in r2 if r.get("round1_worth") != r.get("round2_worth")]
            print(
                f"loop round 2: {len(r2)} follow-ups, {len(flips)} changed worth_keeping"
            )
            for r in r2:
                print(
                    f"  f{r['frame_id']} asked={r.get('asked_for'):<20} "
                    f"imgs={r.get('images_supplied')} "
                    f"{r.get('round1_worth')} -> {r.get('round2_worth')}"
                    f"{'   FLIP' if r.get('round1_worth') != r.get('round2_worth') else ''}"
                )


if __name__ == "__main__":
    main()
