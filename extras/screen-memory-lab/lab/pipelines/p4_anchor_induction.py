"""P4 -- learn the archetypes instead of writing them.

The objection to domain extractors is that the list never ends: a game detector, a
checkout detector, a booking detector, a build detector. But the *shape* of the
screens that matter is not open-ended at all. Applications announce state changes
the same way everywhere: a screen you see briefly, that recurs across the archive
in nearly identical form, sandwiched between longer stretches of something else.
"Victory". "Order placed". "Build failed". "Payment received". "Merged".

So this pipeline induces those screens from the archive with no vocabulary at all:

1. **Cluster** OCR frames into archetypes by text-template similarity, using an
   inverted index over rare tokens so nothing is compared to everything.
2. **Filter** to archetypes that are *transient and recurring* -- they appear in
   several separate islands, each short. That is a purely statistical description
   of a state announcement, and it is what separates "the result screen" from
   "the editor I stare at all day".
3. **Name** the survivors: one model call per archetype, with one image and the
   template text, asking what this screen is, whether it announces a state
   change, and which fields can be read off it. The result is an extractor pack
   -- a durable artifact, keyed to this user's own applications, that later days
   can reuse without paying to rediscover it.
4. **Extract** one event per island of an announcing archetype, with the island's
   own frames as evidence plus the preceding stretch for context.

Cost is front-loaded into induction, which amortises: the pack is reusable, and a
day that contains only already-known archetypes costs one extraction call per
island.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LLM
from ..schema import EVENT_SCHEMA, EXTRACTION_RULES, Event, RunRecord
from ..signals import compact_text
from ..spipe import Frame, jaccard, open_archive, parse_ts, tokens
from ..visual import visual_signals

PACK_DIR = None  # set in run(); defaults to out/packs


@dataclass
class Island:
    archetype: int
    frame_ids: list[int]
    start: str
    end: str

    @property
    def seconds(self) -> float:
        return (parse_ts(self.end) - parse_ts(self.start)).total_seconds()


@dataclass
class Archetype:
    index: int
    frame_ids: list[int]
    template: list[str]
    islands: list[Island] = field(default_factory=list)
    mean_stillness: float = 0.0
    mean_chars: float = 0.0
    days: int = 0
    rank_score: float = 0.0
    name: str = ""
    announces_state: bool = False
    fields: list = field(default_factory=list)
    note: str = ""

    def stats(self) -> dict:
        durations = sorted(i.seconds for i in self.islands)
        median = durations[len(durations) // 2] if durations else 0.0
        return {
            "archetype": self.index,
            "frames": len(self.frame_ids),
            "islands": len(self.islands),
            "median_island_seconds": round(median, 1),
            "days_seen": self.days,
            "mean_stillness": round(self.mean_stillness, 2),
            "mean_chars": round(self.mean_chars),
            "rank_score": round(self.rank_score, 2),
            "template": self.template[:24],
        }


def cluster_archetypes(
    frames: list[Frame],
    min_similarity: float = 0.45,
    min_tokens: int = 5,
    rare_probe: int = 4,
    max_df_ratio: float = 0.25,
) -> list[list[int]]:
    """Single-link clustering of frames by text-template similarity.

    Candidate pairs come from an inverted index over each frame's rarest tokens,
    so this is near-linear rather than quadratic. Tokens that appear in more than
    ``max_df_ratio`` of frames are treated as boilerplate and never used to probe.
    """
    usable = [(f.id, tokens(f.text)) for f in frames if f.text_source == "ocr"]
    usable = [(fid, t) for fid, t in usable if len(t) >= min_tokens]
    if not usable:
        return []

    df: Counter = Counter()
    for _, toks in usable:
        df.update(toks)
    cutoff = max(2, int(len(usable) * max_df_ratio))

    token_map: dict[str, set[int]] = defaultdict(set)
    tok_of: dict[int, set[str]] = {}
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for fid, toks in usable:
        parent[fid] = fid
        tok_of[fid] = toks
        rare = sorted((t for t in toks if df[t] <= cutoff), key=lambda t: df[t])[
            :rare_probe
        ]
        candidates: set[int] = set()
        for t in rare:
            candidates |= token_map[t]
        for other in candidates:
            if find(other) == find(fid):
                continue
            if jaccard(toks, tok_of[other]) >= min_similarity:
                union(fid, other)
        for t in rare:
            token_map[t].add(fid)

    groups: dict[int, list[int]] = defaultdict(list)
    for fid, _ in usable:
        groups[find(fid)].append(fid)
    return [sorted(v) for v in groups.values() if len(v) >= 2]


def build_archetypes(
    frames: list[Frame],
    groups: list[list[int]],
    island_gap_s: float = 120.0,
) -> list[Archetype]:
    by_id = {f.id: f for f in frames}
    visuals = visual_signals(frames)
    out: list[Archetype] = []

    for index, group in enumerate(groups):
        members = [by_id[fid] for fid in group if fid in by_id]
        members.sort(key=lambda f: f.timestamp)
        counts: Counter = Counter()
        for f in members:
            counts.update(tokens(f.text))
        template = [
            t for t, c in counts.most_common(40) if c >= max(2, len(members) * 0.5)
        ]

        arch = Archetype(
            index=index,
            frame_ids=[f.id for f in members],
            template=template,
            mean_stillness=(
                sum(visuals[f.id].stillness for f in members if f.id in visuals)
                / max(1, sum(1 for f in members if f.id in visuals))
            ),
            mean_chars=sum(len(f.text) for f in members) / len(members),
            days=len({f.timestamp.date() for f in members}),
        )
        run_frames: list[Frame] = []
        for f in members:
            if (
                run_frames
                and (f.timestamp - run_frames[-1].timestamp).total_seconds()
                > island_gap_s
            ):
                arch.islands.append(
                    Island(
                        index,
                        [x.id for x in run_frames],
                        run_frames[0].timestamp.isoformat(),
                        run_frames[-1].timestamp.isoformat(),
                    )
                )
                run_frames = []
            run_frames.append(f)
        if run_frames:
            arch.islands.append(
                Island(
                    index,
                    [x.id for x in run_frames],
                    run_frames[0].timestamp.isoformat(),
                    run_frames[-1].timestamp.isoformat(),
                )
            )
        out.append(arch)
    return out


def transient_recurring(
    archetypes: list[Archetype],
    min_islands: int = 2,
    max_median_island_s: float = 240.0,
    max_total_frames: int = 400,
) -> list[Archetype]:
    """Archetypes shaped like a state announcement rather than a workspace.

    Ranking matters as much as filtering. Sorting by island count alone buries
    the screens that appear two or three times a day for ten seconds -- which is
    exactly the shape of a result screen -- underneath screens that flicker into
    view constantly. The score below rewards recurrence *and* brevity, and adds a
    bonus for recurring on more than one day, since a screen seen across days is
    a feature of the applications the user lives in rather than a one-off.
    """
    picked = []
    for a in archetypes:
        if len(a.islands) < min_islands or len(a.frame_ids) > max_total_frames:
            continue
        durations = sorted(i.seconds for i in a.islands)
        median = durations[len(durations) // 2]
        if median > max_median_island_s:
            continue
        gaps = [
            (parse_island(b.start) - parse_island(a2.end)).total_seconds()
            for a2, b in zip(a.islands, a.islands[1:])
        ]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
        # Brief, repeated, and separated by long stretches of something else.
        a.rank_score = (
            len(a.islands)
            * (1.0 + 60.0 / (median + 20.0))
            * (1.0 + min(median_gap, 3600.0) / 3600.0)
            * (1.0 + 0.5 * (a.days - 1))
        )
        picked.append(a)
    picked.sort(key=lambda a: -a.rank_score)
    return picked


def parse_island(iso: str):
    return parse_ts(iso)


NAME_PROMPT = """This screen recurs in a personal screen-capture archive. It appeared in
{islands} separate short episodes over {days} day(s), each lasting about
{median}s, and then went away again.

Words that appear on essentially every instance of it:
{template}

Full text of one instance (frame {frame_id}):
{text}

An image of that instance is attached.

Describe this screen as a reusable detector.

Name and describe the screen by the SLOTS it has, never by the values this
instance happens to show. "match result summary" is a correct name; "defeat
summary" is not, because the same screen shows a win tomorrow and a detector
named after one outcome makes every later instance report that outcome. The same
applies to `what_it_announces` and to every entry in `fields`: say *what is
reported there*, not *what it said this time*. For any slot whose value varies
between instances, set `varies: true`.

Return JSON:
{{
  "name": "short lowercase name for this kind of screen, e.g. 'match result summary'",
  "application": "what application or site shows it, or 'unknown'",
  "announces_state_change": true|false,
  "what_it_announces": "the kind of outcome or state it reports, or null",
  "fields": [{{"field": "name", "how_to_read": "where on this screen the value appears", "varies": true|false}}],
  "preceded_by": "what screen normally comes before it, if the evidence suggests one",
  "worth_extracting": true|false,
  "why": "one sentence"
}}

`announces_state_change` is true only if the screen reports that something
finished, succeeded, failed, was confirmed, or changed state -- not merely that a
new view was opened. Judge from what is on the screen, not from what you assume
the application is for."""

EXTRACT_PROMPT = """{rules}

A learned detector fired. This is the extractor pack entry:
{pack}

It matched frames {frame_ids} between {start} and {end} UTC.

Text of the matched frames:
{matched}

The stretch of capture leading up to it, for context ({lead_start} to {start}):
{lead}

{image_note}

Produce the event this announces. Use the pack only to know WHICH fields to look
for; read every VALUE from the matched frames and the context below. If the pack's
wording implies a particular outcome, ignore that implication -- the pack was
written from a different instance of this screen.

Take care over who a stated outcome belongs to. A screen that says "Defeat"
usually reports the result for the person using the computer, not for the opponent
named on it; check the surrounding lines before assigning it. If the context shows this is the second such event in a
row, the event is only the current one -- do not merge them. Return JSON:
{{"events": [{schema}]}}"""


def run(
    start: str,
    end: str,
    model: str = "gpt-5.4-mini",
    effort: str = "low",
    induce_from: tuple[str, str] | None = None,
    max_archetypes: int = 30,
    lead_minutes: float = 12.0,
    image_per_island: bool = True,
    reuse_pack: str | None = None,
) -> RunRecord:
    began = time.time()
    archive = open_archive()
    llm = LLM(model=model, effort=effort)

    # Induction may look at a wider period than extraction: recurrence is easier
    # to see across days, and the pack is meant to be reused.
    ind_start, ind_end = induce_from or (start, end)
    induction_frames = archive.frames(ind_start, ind_end)
    frames = archive.frames(start, end)

    trace: list[dict] = []
    images_used = 0

    if reuse_pack:
        pack = json.loads(open(reuse_pack).read())
        trace.append({"stage": "pack", "reused_from": reuse_pack})
        groups = None
        archetypes = []
    else:
        groups = cluster_archetypes(induction_frames)
        archetypes = build_archetypes(induction_frames, groups)
        candidates = transient_recurring(archetypes)[:max_archetypes]
        trace.append(
            {
                "stage": "cluster",
                "frames": len(induction_frames),
                "clusters": len(groups),
                "candidate_archetypes": [a.stats() for a in candidates],
            }
        )

        pack = {"entries": []}
        for arch in candidates:
            rep = max(
                (fid for isl in arch.islands for fid in isl.frame_ids),
                key=lambda fid: len(
                    (archive.frame(fid).text if archive.frame(fid) else "")
                ),
            )
            rep_frame = archive.frame(rep)
            images = []
            try:
                images.append(archive.frame_png(rep, 1280))
                images_used += 1
            except Exception:
                pass
            durations = sorted(i.seconds for i in arch.islands)
            described = llm.json_complete(
                NAME_PROMPT.format(
                    islands=len(arch.islands),
                    days=arch.days,
                    median=round(durations[len(durations) // 2]),
                    template=", ".join(arch.template[:30]),
                    frame_id=rep,
                    text=(rep_frame.text[:2500] if rep_frame else ""),
                ),
                images=images or None,
            )
            if not isinstance(described, dict):
                continue
            entry = {
                **described,
                "archetype": arch.index,
                "stats": arch.stats(),
                "islands": [
                    {"frame_ids": i.frame_ids, "start": i.start, "end": i.end}
                    for i in arch.islands
                ],
            }
            pack["entries"].append(entry)
            trace.append(
                {
                    "stage": "name",
                    "archetype": arch.index,
                    "name": described.get("name"),
                    "announces": described.get("announces_state_change"),
                    "worth_extracting": described.get("worth_extracting"),
                }
            )

    # ------------------------------------------------------------- extraction
    events: list[Event] = []
    day_lo = frames[0].timestamp if frames else None
    for entry in pack["entries"]:
        if not (entry.get("announces_state_change") and entry.get("worth_extracting")):
            continue
        for island in entry.get("islands", []):
            ids = [int(i) for i in island["frame_ids"]]
            in_range = [f for f in frames if f.id in ids]
            if not in_range:
                continue  # island belongs to another day of the induction period
            lead_start = max(
                day_lo,
                in_range[0].timestamp
                - __import__("datetime").timedelta(minutes=lead_minutes),
            )
            lead = [
                f for f in frames if lead_start <= f.timestamp < in_range[0].timestamp
            ]

            images = []
            if image_per_island:
                best = max(in_range, key=lambda f: len(f.text))
                try:
                    images.append(archive.frame_png(best.id, 1280))
                    images_used += 1
                except Exception:
                    pass

            payload = llm.json_complete(
                EXTRACT_PROMPT.format(
                    rules=EXTRACTION_RULES,
                    pack=json.dumps(
                        {
                            k: v
                            for k, v in entry.items()
                            if k not in ("islands", "stats")
                        },
                        indent=1,
                    ),
                    frame_ids=ids,
                    start=island["start"],
                    end=island["end"],
                    matched=compact_text(in_range)[:8000],
                    lead=compact_text(lead)[:14000],
                    lead_start=lead_start.isoformat(),
                    image_note=(
                        "An image of the matched screen is attached."
                        if images
                        else "No image available."
                    ),
                    schema=EVENT_SCHEMA,
                ),
                images=images or None,
            )
            found = payload.get("events", []) if isinstance(payload, dict) else []
            events.extend(
                Event.from_model(e, f"archetype-{entry['archetype']}") for e in found
            )
            trace.append(
                {
                    "stage": "extract",
                    "archetype": entry["archetype"],
                    "island": [island["start"], island["end"]],
                    "events": len(found),
                }
            )

    packs = Path(__file__).resolve().parents[2] / "out" / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    pack_path = packs / f"pack-{time.strftime('%Y%m%d-%H%M%S')}.json"
    pack_path.write_text(json.dumps(pack, indent=2))

    return RunRecord(
        pipeline="p4_anchor_induction",
        params={
            "model": model,
            "effort": effort,
            "induce_from": [ind_start, ind_end],
            "max_archetypes": max_archetypes,
            "pack": str(pack_path),
            "reused_pack": reuse_pack,
        },
        events=events,
        usage=llm.usage.summary(),
        trace=trace,
        wall_seconds=time.time() - began,
        frames_considered=len(frames),
        frames_read_as_text=len(frames),
        frames_viewed_as_image=images_used,
        notes=(
            f"Induced {len(pack['entries'])} archetypes; "
            f"{sum(1 for e in pack['entries'] if e.get('announces_state_change'))} announce state. "
            f"Pack written to {pack_path.name}."
        ),
    )
