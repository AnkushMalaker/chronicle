"""Score VLM captions as a RETRIEVAL index, against connector-supplied truth.

The tier-1 test from docs/research/screen-memory/07-how-real-systems-do-it.md.
Nothing here asks a model whether an event happened. The question is only:

    given the user's own question, does the right MOMENT come back?

Ground truth needs no hand labelling and no LLM judge -- the AoE4World API says
exactly when each match started and how long it ran, so a frame either falls
inside a real match or it does not. That is the point of tier 2: it labels tier 1
for free.

Four index variants are compared through the *same* retriever, so the comparison
is about index content and not about retrieval technology:

    ocr          stored ScreenPipe OCR text (the existing baseline)
    caption      E2B prose description
    caption_text E2B prose + prominent text copied verbatim
    ocr+caption  union

Retriever is plain BM25. A production system would use embeddings; using one
retriever for all four variants keeps the difference attributable to the text.

Run:
    uv run python vlm_bench/retrieve_eval.py \
        --captions out/vlm/captions_e2b.jsonl --label e2b
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID_SETS = ["cap_w1", "cap_w2", "cap_w3"]
KNOWN_SET = "cap_known"

# From lab/groundtruth.py AUTHORITATIVE_RECORD -- the five matches the archive
# captured, as reported by the AoE4World API. (start_utc, duration_s, label)
MATCHES = [
    ("2026-07-24T15:01:20", 718, "m1 Marshland win"),
    ("2026-07-24T15:27:50", 119, "m2 Mountain Clearing loss"),
    ("2026-07-24T21:42:16", 1040, "m3 Golden Pit loss"),
    ("2026-07-24T22:01:51", 1539, "m4 Himeyama win"),
    ("2026-07-25T16:39:40", 1747, "m5 MegaRandom win"),
]
# The result banner and post-match summary land at or just after the API's end
# time, and they are the frames we most want to retrieve. Windows are extended by
# this much. Reported both ways so the choice is visible rather than buried.
POST_MATCH_GRACE_S = 120

QUERIES = {
    # The user's actual question, as they would type it.
    "user_question": "which Age of Empires games did I win and lose",
    # Same intent, keyword shaped.
    "keywords": "age of empires victory defeat won lost match result",
    # Outcome words only -- tests whether the index carries the banner text.
    "outcome_only": "victory defeat",
}

# Frames whose real content is known, used for the two checks that a clock grid
# cannot make. Not part of any precision number.
KNOWN = {
    7064: ("m1 VICTORY banner", "result"),
    7173: ("m2 DEFEAT banner, ZERO stored OCR", "result"),
    8577: ("m3 DEFEAT banner", "result"),
    8981: ("m4 VICTORY banner", "result"),
    10697: ("m5 post-match Victory summary", "result"),
    10699: ("m5 Victory", "result"),
    10210: ("Wyzvok's profile page - ANOTHER PLAYER's results", "trap"),
    10169: ("my own terminal output about this investigation", "trap"),
    10878: ("the review viewer I built", "trap"),
}
# Words that would show a caption correctly named the screen, per trap frame.
TRAP_EXPECT = {
    10210: ["profile", "player card", "career", "wyzvok"],
    10169: ["terminal", "console", "command line", "shell", "text output"],
    10878: ["browser", "web page", "webpage", "list", "table", "viewer", "review"],
}

TOKEN = re.compile(r"[a-z0-9]+")


def toks(s: str) -> list[str]:
    return TOKEN.findall((s or "").lower())


class BM25:
    def __init__(self, docs: dict[int, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids = list(docs)
        self.tf = {i: Counter(toks(docs[i])) for i in self.ids}
        self.len = {i: sum(self.tf[i].values()) for i in self.ids}
        n = len(self.ids) or 1
        self.avg = (sum(self.len.values()) / n) or 1.0
        df = Counter()
        for i in self.ids:
            df.update(self.tf[i].keys())
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def rank(self, query: str) -> list[tuple[int, float]]:
        q = toks(query)
        out = []
        for i in self.ids:
            tf, dl = self.tf[i], self.len[i]
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                s += (
                    self.idf.get(t, 0.0)
                    * (f * (self.k1 + 1))
                    / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
                )
            if s > 0:
                out.append((i, s))
        # Deterministic: score desc, then frame id asc.
        out.sort(key=lambda kv: (-kv[1], kv[0]))
        return out


def windows(grace: int) -> list[tuple[datetime, datetime, str]]:
    ws = []
    for start, dur, label in MATCHES:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        ws.append((s, s + timedelta(seconds=dur + grace), label))
    return ws


def which_match(ts: datetime, ws) -> str | None:
    for s, e, label in ws:
        if s <= ts <= e:
            return label
    return None


def load_frames(sets: list[str]) -> dict[int, dict]:
    frames = {}
    for name in sets:
        mp = ROOT / "out" / "frames" / name / "manifest.json"
        for m in json.loads(mp.read_text()):
            if not m.get("png"):
                continue
            frames[m["frame_id"]] = {
                "utc": datetime.fromisoformat(m["utc"]),
                "ocr": m.get("ocr_text") or "",
                "set": name,
                "local": m.get("local"),
            }
    return frames


def load_captions(path: Path) -> tuple[dict[str, dict[int, str]], dict[str, dict]]:
    """Captions by prompt, plus per-prompt attempt/failure counts.

    A prompt whose captions are incomplete cannot be compared head-to-head with
    one that is, so the counts are returned and printed rather than swallowed --
    the Gemini run hit HTTP 402 (out of credit) partway through its second prompt,
    and scoring that silently would have looked like a model weakness.
    """
    caps: dict[str, dict[int, str]] = {}
    stats: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        st = stats.setdefault(
            r["prompt"], {"attempted": 0, "failed": 0, "errors": Counter()}
        )
        st["attempted"] += 1
        if not r.get("ok"):
            st["failed"] += 1
            st["errors"][(r.get("error") or "")[:60]] += 1
            continue
        caps.setdefault(r["prompt"], {})[r["frame_id"]] = r.get("text", "")
    for pname, st in stats.items():
        if st["failed"]:
            top = st["errors"].most_common(1)[0]
            print(
                f"  !! {pname}: {st['failed']}/{st['attempted']} calls FAILED "
                f"-- {top[0]!r} (x{top[1]}). This index is INCOMPLETE; do not "
                f"compare it head-to-head."
            )
    return caps, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--captions", required=True)
    ap.add_argument("--label", default="vlm")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    caps, capstats = load_captions(Path(args.captions))
    grid = load_frames(GRID_SETS)
    known = load_frames([KNOWN_SET])
    ws = windows(POST_MATCH_GRACE_S)
    strict = windows(0)

    in_match = {i: which_match(f["utc"], ws) for i, f in grid.items()}
    n_in = sum(1 for v in in_match.values() if v)
    print(f"\n=== {args.label} ===")
    print(
        f"grid: {len(grid)} frames, {n_in} inside a real match "
        f"({n_in/len(grid)*100:.0f}%), {len(grid)-n_in} outside. "
        f"Random top-{args.k} precision would be ~{n_in/len(grid)*100:.0f}%."
    )
    print(f"known frames: {len(known)}")

    variants: dict[str, dict[int, str]] = {
        "ocr": {i: f["ocr"] for i, f in grid.items()},
    }
    partial = set()
    for pname, byframe in caps.items():
        covered = sum(1 for i in grid if byframe.get(i))
        tag = pname
        if covered < len(grid):
            tag = f"{pname}*"
            partial.add(tag)
            partial.add(f"ocr+{tag}")
        variants[tag] = {i: byframe.get(i, "") for i in grid}
        variants[f"ocr+{tag}"] = {
            i: (grid[i]["ocr"] + " \n " + byframe.get(i, "")) for i in grid
        }
        print(f"  {pname}: covers {covered}/{len(grid)} grid frames")
    if partial:
        print("  (* = incomplete index, numbers are a floor not a fair comparison)")

    # ---- Metric 1: does the right moment come back? -----------------------
    print(f"\n-- retrieval on the {len(grid)}-frame clock grid, top-{args.k} --")
    hdr = (
        f"{'index':<20} {'query':<14} {'P@k':>7} {'matches hit':>12} {'empty docs':>11}"
    )
    print(hdr)
    print("-" * len(hdr))
    results = {}
    for vname, docs in variants.items():
        bm = BM25(docs)
        n_empty = sum(1 for d in docs.values() if not toks(d))
        for qname, q in QUERIES.items():
            ranked = bm.rank(q)[: args.k]
            hits = [i for i, _ in ranked if in_match[i]]
            found = {in_match[i] for i, _ in ranked if in_match[i]}
            p = len(hits) / max(1, len(ranked))
            results[(vname, qname)] = (p, found, len(ranked))
            print(
                f"{vname:<20} {qname:<14} {p*100:>6.0f}% "
                f"{len(found)}/5{'':>8} {n_empty:>10}"
            )

    print("\n-- which matches each index finds (user_question, top-%d) --" % args.k)
    for vname in variants:
        _, found, _ = results[(vname, "user_question")]
        miss = [m[2] for m in MATCHES if m[2] not in found]
        print(
            f"  {vname:<20} found {len(found)}/5" + (f", missed {miss}" if miss else "")
        )

    # ---- Metric 1b: can you tell WHO WON? --------------------------------
    # Being inside a match is not the same as being able to answer the question.
    # The outcome is only legible near the end, where the result banner and the
    # post-match summary live. This is the metric that matches the user's actual
    # question, and it is strictly harder.
    out_win = []
    for start, dur, label in MATCHES:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        e = s + timedelta(seconds=dur)
        out_win.append((e - timedelta(seconds=60), e + timedelta(seconds=180), label))
    in_out = {i: which_match(f["utc"], out_win) for i, f in grid.items()}
    n_out = sum(1 for v in in_out.values() if v)
    print(
        f"\n-- metric 1b: OUTCOME-legible frames (match end -60s..+180s) --\n"
        f"  {n_out}/{len(grid)} grid frames could show a result "
        f"({n_out/len(grid)*100:.0f}% base rate)"
    )
    for vname, docs in variants.items():
        bm = BM25(docs)
        for qname in ("user_question", "outcome_only"):
            ranked = bm.rank(QUERIES[qname])
            top = ranked[: args.k]
            hits = [i for i, _ in top if in_out[i]]
            eps = {in_out[i] for i, _ in top if in_out[i]}
            # How deep must you go to cover all five outcomes?
            covered, depth = set(), None
            for n, (i, _) in enumerate(ranked, 1):
                if in_out[i]:
                    covered.add(in_out[i])
                if len(covered) == 5:
                    depth = n
                    break
            print(
                f"  {vname:<20} {qname:<14} P@{args.k} {len(hits)/max(1,len(top))*100:>5.0f}%"
                f"   outcomes in top-{args.k}: {len(eps)}/5"
                f"   depth for 5/5: {depth if depth else '>'+str(len(ranked))}"
            )

    # ---- Metric 2: the zero-OCR banner -----------------------------------
    print("\n-- metric 2: can the zero-OCR DEFEAT banner be found at all? --")
    all_frames = {**grid, **known}
    for vname in ["ocr"] + list(caps):
        docs = {}
        for i, f in all_frames.items():
            if vname == "ocr":
                docs[i] = f["ocr"]
            else:
                docs[i] = caps[vname].get(i, "")
        bm = BM25(docs)
        ranked = bm.rank(QUERIES["outcome_only"])
        pos = {fid: n + 1 for n, (fid, _) in enumerate(ranked)}
        line = []
        for fid in (7173, 7064, 8577, 8981, 10697):
            r = pos.get(fid)
            if r:
                line.append(f"f{fid}=#{r}")
            elif not (docs.get(fid) or "").strip():
                # No document at all -- a failed call, not a model miss.
                line.append(f"f{fid}=NO-DOC")
            else:
                line.append(f"f{fid}=no-match")
        print(f"  {vname:<14} " + "  ".join(line))
    print(
        "     (rank for query 'victory defeat' over all "
        f"{len(all_frames)} frames; 'no-match' = doc exists but lacks the query "
        "terms; 'NO-DOC' = the captioning call never produced text)"
    )

    # ---- Metric 3: do captions name the traps correctly, unprompted? -----
    print("\n-- metric 3: trap frames, described WITHOUT any authorship rule --")
    for fid, (what, kind) in KNOWN.items():
        if kind != "trap":
            continue
        print(f"\n  f{fid} — actually: {what}")
        for pname in caps:
            text = caps[pname].get(fid, "")
            low = text.lower()
            wanted = TRAP_EXPECT.get(fid, [])
            named = [w for w in wanted if w in low]
            # A caption that claims the *user* won/lost is the failure mode.
            claims = [
                w
                for w in ("i won", "you won", "your victory", "the player won")
                if w in low
            ]
            verdict = "NAMES IT" if named else "does not name it"
            print(
                f"    [{pname}] {verdict} {named or ''}"
                + (f"  CLAIM-LEAK {claims}" if claims else "")
            )
            print(f"      {' '.join(text.split())[:400]}")

    # ---- Extras ----------------------------------------------------------
    print("\n-- caption vs OCR coverage on the grid --")
    ocr_empty = sum(1 for f in grid.values() if not toks(f["ocr"]))
    print(f"  frames with empty stored OCR: {ocr_empty}/{len(grid)}")
    for pname in caps:
        got = sum(1 for i in grid if toks(caps[pname].get(i, "")))
        print(f"  frames with a usable {pname}: {got}/{len(grid)}")

    print("\n-- strict windows (no post-match grace), user_question --")
    in_strict = {i: which_match(f["utc"], strict) for i, f in grid.items()}
    for vname, docs in variants.items():
        ranked = BM25(docs).rank(QUERIES["user_question"])[: args.k]
        hits = [i for i, _ in ranked if in_strict[i]]
        print(f"  {vname:<20} P@{args.k} {len(hits)/max(1,len(ranked))*100:>5.0f}%")

    out = ROOT / "out" / "vlm" / f"retrieval_{args.label}.json"
    out.write_text(
        json.dumps(
            {
                "label": args.label,
                "grid_frames": len(grid),
                "frames_in_match": n_in,
                "k": args.k,
                "results": {
                    f"{v}|{q}": {"p_at_k": p, "matches_found": sorted(f)}
                    for (v, q), (p, f, _) in results.items()
                },
            },
            indent=1,
        )
    )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
