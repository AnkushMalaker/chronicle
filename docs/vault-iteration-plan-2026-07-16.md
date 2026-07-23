# Vault Iteration Plan — 2026-07-16

Follow-up to `memory-rebuild-audit-2026-07-16.md`. Two inputs were produced:

1. **Transcript categorization** of all 169 vault-eligible conversations
   (gpt-4.1-mini, artifacts in `backends/advanced/data/transcript_categorization/`).
2. **Deep fidelity audit** of 8 good conversations (transcript vs vault note vs
   linked entity notes) plus a whole-vault structural audit, run as three
   independent read-only agent passes over rebuild `970899d4dea6`.

## 1. Categorization result (the workable dataset)

`workable_dataset.json` tiers the 169 conversations:

| Tier | Count | Audio | Meaning |
|---|---|---|---|
| keep_core | 130 | 26.8 h | real conversations; 80 rated memory-worthiness 2 |
| keep_context | 20 | 0.6 h | Hermes/HA voice-command sessions — useful signal, kept and labeled |
| low_value | 14 | 1.0 h | fragments (11) + background media (3), nothing memorable |
| prune | 5 | 4.1 h | pure ASR hallucination (repeated-token loops, control tokens) |

Flags: 32 keep_core conversations contain hallucinated ASR spans inside real
speech (`needs_transcript_cleanup` list); 42 contain HA commands mixed into real
conversation. The 5 prune conversations are `103910ee`, `12db218d`, `1d349639`,
`31649f4e`, `4096782e` — together 4.1 h of audio, and the direct source of the
fabricated `Hermes Chronicle`, `India`, `Myanmar`, `film`, `product` topic notes.

## 2. State of the vault on good cases

Eight high-worthiness conversations were audited fact-by-fact (4 technical/work:
`5c0b2333`, `99e2a020`, `7620c7b4`, `ea282e40`; 4 personal Hindi-English:
`a4ed37ac`, `19f5a281`, `d0de4521`, `fd3f7f7d`). Grades ranged C to B.

**What works now (post-remediation baseline):**

- **Zero invented facts across all 8 notes.** Every Key Fact traced to the
  transcript. On garbage-ASR input the agent correctly stays vague instead of
  hallucinating (`fd3f7f7d`, `99e2a020`). This conservatism is the foundation to
  build on.
- Frontmatter date/duration metadata is now exact (10/10 spot-checks matched to
  the microsecond).
- `People/ankush.md` and `People/Anushpa.md` rollups are accurate and are the one
  reliable retrieval surface today.
- Action items, where populated, are mostly real commitments (best case
  `7620c7b4`: 5/5 grounded).

**Failure modes, in order of damage:**

1. **Under-coverage (~half the substance dropped), biased toward the safe
   surface.** Notes summarize the opening topic and truncate. What gets dropped
   is precisely what a personal-memory system exists for:
   - `a4ed37ac`: omits that ankush's father is not attending his wedding and the
     family estrangement around it (~40% of the conversation).
   - `19f5a281`: omits the genetic marker → no synthetic folic acid health fact.
   - `d0de4521`: omits ankush being sick 4-5 days, Prayal visiting, his
     journaling/meds-reminder workflow, and two real helping-commitments (set up
     the maid's UPI account, renew जितबहादुर's insurance) — the Action Items
     section shipped as an empty `- [ ]`.
   - `5c0b2333`: omits the entire FDE-role/career half, Vipul's bio
     (ex-Deutsche-Bank, 5 yrs), and the Verizon/JPMC/Morgan-Stanley context.
   - `ea282e40`: omits the actual point of the call — co-founder Asif's flawed
     email and the angry-customer escalation dynamic; Asif appears nowhere.
2. **Attribution smears from broken diarization** — the top correctness lever
   given inventions are zero. `ea282e40` assigns Tanmay's A100/H100 conclusion to
   Shiva (the customer who was pushing back); `7620c7b4` turns Vipul's RabbitMQ
   hypothesis into Ankush's assertion; `d0de4521` collapses two distinct third
   parties into "the person."
3. **Titles: 9 notes use the raw first ASR line** ("So, the chronicle. I forgot.
   Yeah," / "Question. Do you-- whenever you have") and ~15-20 more are fully
   generic. Grep test: zero titles contain "Hermes" (59 linked conversations) or
   "Verizon" (a whole escalation call). Title-based retrieval is broken.
4. **Topic layer is unreliable in both directions.** 91 distinct dangling link
   targets (157 occurrences, e.g. all 8 topics of `19f5a281`); 25/127 topic notes
   are empty stubs (including `family`, the dominant theme of the personal
   corpus); spurious tags (`[[latency]]`, `[[UI design]]` from a "UI team" Slack
   channel); and **cross-conversation rollup contamination** — `Topics/token
   sampling.md` and `People/Rishon.md` attribute a different same-day
   conversation's facts to `99e2a020`.
5. **People notes waste their structured fields.** All 14 have empty
   `org/role/relationship/location` even when the transcript states them
   (Vipul = ex-DB ML engineer reporting to Kerry+Anurag; Shiva Barathi = the
   Verizon customer). Person-name mentions (Kevin Jacob, Priyanka, Asif, Tanmay)
   are never promoted to People notes, while one-off trivia ("bong water",
   "quantum knowledge") do become Topic files — the promotion heuristic is
   backwards. 11 entity notes are orphans with zero inbound links.
6. **Junk and command noise pollute the graph.** All 16 junk conversations got
   full template notes indistinguishable from real ones; 7+ entity notes exist
   only because of hallucinated audio (`Hermes Chronicle`, `Sunil`, …).
   `Topics/Hermes.md` (81 link occurrences) buries real wake-word development
   narrative under dozens of "Hey Hermes, turn off study lights" log lines;
   `Topics/study.md` and `hall.md` are ~100% command noise.

## 3. Iteration plan (ranked, mapped to pipeline)

1. **Ingestion category gate** — feed `workable_dataset.json` tiers into the
   rebuild path (`providers/chronicle.py`): skip the 5 prune conversations
   entirely; render the 20 assistant_command conversations as a compact
   device-interaction log (timestamp, command, target, outcome) instead of full
   Summary/Key-Facts notes; stamp `category` + `memory_worthiness` into
   conversation frontmatter so the memory agent can weight trust. Longer-term,
   run the same gpt-4.1-mini classifier as a standing transcript-quality gate
   before memory jobs (`categorize.py` is reusable as-is).
2. **Speaker reconciliation before summarization** — merge fragmented
   `Unknown Speaker N` labels per conversation before the transcript reaches
   `MemoryAgent.run`, and instruct the agent to mark attribution as uncertain
   rather than pick a name; misattributed health/finance/conflict facts are the
   highest-damage error class observed.
3. **Coverage pass for long transcripts** — the agent summarizes the first third
   and stops. Chunk long transcripts (map-reduce or sectioned outline) and add
   explicit extraction targets weighted above logistics: health facts,
   relationships/conflicts, life events, stated commitments, and durable bio
   facts about people (feed these into the People frontmatter fields). Ban empty
   `- [ ]` placeholders — omit the section instead.
4. **Semantic titles with validation** — reject any title that is a prefix of the
   transcript's first line or matches the generic-placeholder set; require
   named-entity-bearing titles (`vault` validation in `providers/chronicle.py`
   already has the hook point; this extends the existing placeholder check).
5. **Entity-layer integrity** — one mechanism, several symptoms: (a) every
   emitted `[[topic]]` must either resolve or auto-create a note with an About
   line (no more 91 dangling targets), (b) rollups must carry per-conversation
   provenance so two same-day conversations can't blend, (c) promote person-name
   mentions to People stubs and stop promoting one-off trivia to Topics
   (frequency ≥ 2 or explicit-salience rule), (d) snap topics to a controlled
   vocabulary to kill the `weddings` / `wedding planning`, `dairy` × 4 drift.
6. **One-time cleanup of the current vault** (cheap, no rebuild): delete the 7
   junk-provenance entity notes, merge the 10 duplicate topic clusters, retitle
   the 9 raw-ASR notes, reclassify `Samir` as a person, and fill the 14 People
   frontmatter blocks from facts already present in their bodies.

Items 1, 4, and 6 are mechanical. Items 2, 3, 5 change memory-agent behavior and
should be validated on the 8 audited conversations (grades above are the
baseline) before any full replay.
