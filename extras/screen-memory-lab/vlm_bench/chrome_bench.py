"""Does a VLM harvest the navigational chrome? Three-way against hand truth.

Report 13 §4 found that the durable, identity-bearing facts on screen sit in the
chrome -- subscription lists, tab strips, thread lists, library lists, nameplates
-- and that the stored text indexes miss them. §7 proposed testing whether a VLM
can enumerate them. This is that test.

Three readers over the same frames:

    hand     what a human read off the PNG (vlm_bench/chrome_truth.py),
             written down BEFORE the prompt below existed
    text     the frame's stored text -- OCR or accessibility, whichever
             ScreenPipe recorded. The free baseline.
    chrome   a VLM asked to ENUMERATE rather than describe

The `caption` prompt is also scored, to check the obvious objection: maybe the
retrieval caption already contains this and no new prompt is needed. It should
not, because `caption` asks for 2-4 sentences of prose and prose does not
enumerate a 29-game library.

    uv run python -m vlm_bench.chrome_bench --model gpt-5.6-sol
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from lab.spipe import Archive
from vlm_bench.caption_prompts import PROMPTS
from vlm_bench.chrome_truth import FRAMES, contains

OUT = Path(__file__).resolve().parent.parent / "out" / "vlm"

# Deliberately an ENUMERATION prompt, not a description prompt. It names the
# surfaces to look at, because "list what you see" invites the model to list the
# content instead -- which is what `caption` already does well.
CHROME_PROMPT = """\
List the named items in this screenshot's INTERFACE FURNITURE -- not the content \
being viewed.

Look specifically at: browser tab strips, sidebars, subscription or channel lists, \
library or file lists, navigation bars and their counters, conversation or thread \
lists, window titles, account names, and player nameplates.

Copy names EXACTLY as written, including punctuation, capitalisation and handles. \
Do not correct or expand them. Do not guess at text you cannot read -- omit it.

Reply with ONLY a JSON object, no prose and no code fence:
{"app": "<application or site>", "account": "<signed-in user, or null>", \
"tabs": [], "sidebar_items": [], "nav_items": [], "counters": {"<label>": "<value>"}, \
"other_named": []}
"""


def run(png: Path, prompt: str, model: str, timeout: int = 240) -> tuple[str, float]:
    t0 = time.time()
    p = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", "-m", model, "-i", str(png)],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # stdout is the clean answer; stderr is the transcript. See report 10 §1.
    return p.stdout.strip(), time.time() - t0


def score(blob: str, truth: dict) -> dict:
    ents = [e for e in truth["entities"] if contains(blob, e)]
    facts = [f for f in truth["facts"] if contains(blob, f)]
    return {
        "entities_found": len(ents),
        "entities_total": len(truth["entities"]),
        "facts_found": len(facts),
        "facts_total": len(truth["facts"]),
        "missed_entities": [e for e in truth["entities"] if e not in ents],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--out", type=Path, default=OUT / "chrome_bench.json")
    args = ap.parse_args()

    arch = Archive()
    rows, raw = [], {}
    for fid, truth in FRAMES.items():
        png = arch.frame_png(fid, max_width=1600)
        stored = arch.frame(fid).text or ""

        chrome_out, t_chrome = run(png, CHROME_PROMPT, args.model)
        caption_out, t_cap = run(png, PROMPTS["caption"], args.model)
        raw[fid] = {"chrome": chrome_out, "caption": caption_out}

        row = {
            "frame": fid,
            "what": truth["what"],
            "stored_chars": len(stored),
            "text": score(stored, truth),
            "chrome": score(chrome_out, truth),
            "caption": score(caption_out, truth),
            "secs": round(t_chrome + t_cap, 1),
        }
        rows.append(row)

        print(f"\n=== {fid}  {truth['what']}")
        print(f"    stored text: {len(stored)} chars")
        for k in ("text", "caption", "chrome"):
            s = row[k]
            print(
                f"    {k:<8} entities {s['entities_found']:>2}/{s['entities_total']:<2}"
                f"  facts {s['facts_found']:>2}/{s['facts_total']}"
            )
        if row["chrome"]["missed_entities"]:
            print(f"    chrome missed: {row['chrome']['missed_entities']}")

    print("\n" + "=" * 68)
    for k in ("text", "caption", "chrome"):
        e = sum(r[k]["entities_found"] for r in rows)
        et = sum(r[k]["entities_total"] for r in rows)
        f = sum(r[k]["facts_found"] for r in rows)
        ft = sum(r[k]["facts_total"] for r in rows)
        print(
            f"{k:<8} entities {e:>3}/{et}  ({e/et*100:>4.0f}%)   "
            f"facts {f:>2}/{ft}  ({f/ft*100:>4.0f}%)"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"model": args.model, "rows": rows, "raw": raw}, indent=1)
    )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
