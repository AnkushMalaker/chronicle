# Memory Rebuild Audit - 2026-07-16

## Scope

Read-only audit of the live Chronicle database and generated vault after rebuild
`b5982bbd1fb4`.

- Vault: `backends/advanced/data/conversation_docs/69b80e5894aa9ec334a421c9`
- Source: live MongoDB `chronicle` database
- Archive: `backends/advanced/data/backups/chronicle_20260715_203124.chronicle`
- Eligible source conversations: 169
- Generated conversation paths: 169

The earlier `169 notes, 0 missing, 0 empty` check only established that each expected
path existed and had nonzero bytes. It did not validate Markdown schema, semantic
content, source metadata, speaker accuracy, or whether a note was an empty template.

## Verdict

The rebuilt vault is not usable as a clean memory reconstruction. The defects are
systemic and originate in speaker identification, transcript segment assembly, memory
agent output validation, and missing source metadata. Do not treat the current vault as
a faithful derivative of the imported conversations.

## Confirmed Findings

### Conversation notes

- 169/169 expected filenames exist, with no extra conversation filenames.
- Only 13/169 start with parseable YAML frontmatter.
- Only 7/169 have valid leading frontmatter with the exact filename-matching
  `conversation_id`.
- 124 are heading-only notes with no usable frontmatter.
- 18 are entirely enclosed in Markdown code fences.
- 11 start directly with `categories:` and omit the YAML opener.
- 3 contain literal `\n`, `+++`, or similarly malformed serialization.
- 32/169 fail the required Summary / Key Facts / Action Items section contract.
- 44 files are at most 400 bytes.
- 18 have an empty Summary and 15 have empty Key Facts.
- 13 notes collapse into two identical empty-template bodies after metadata/title removal.
- All source conversations have a date and audio duration. Every generated date is
  incorrect: 106 notes omit it and all dates present use the rebuild date rather than
  `created_at`. No note contains a valid numeric duration; 109 omit it and 60 use
  `unknown`.
- 58 titles are placeholders or unusable, including `Unknown Conversation`,
  `Unnamed Conversation`, UUID-only titles, and literal `<title>`.

Examples:

- `Conversations/2f22d2b5-9f51-4645-8d77-9ce2c79fa2c8.md` is an empty `<title>`
  template for a 5,612-character source transcript.
- `Conversations/5c0b2333-acb6-4c45-800d-0d3d3ad0cb91.md` is an empty `<title>`
  template for a 29,044-character source transcript.
- `Conversations/7620c7b4-c6c4-433d-b2c3-4404ff5346b9.md` is an empty UUID-titled
  template for a 30,136-character source transcript.
- `Conversations/beb6c35c-ede3-41dc-9fac-1f0898f65296.md` contains a corrupted
  internal conversation ID.

### Content quality

- 130/169 conversation notes contain nonempty action items.
- 396 populated checklist actions were generated.
- 288 use generic speculative follow-up verbs.
- In 261, the action verb does not occur in the source transcript. This is a heuristic
  warning rather than proof by itself, but manual samples confirm invented tasks.
- 29 conversation notes contain 305 exact duplicate substantive lines.
- `1be751bc-d82e-4c0d-99c5-02fe0359dc5d.md` contains 111 action items; two lines are
  repeated 49 and 48 times.
- `f1c214c4-6f46-48c8-a58a-6445b2da27f1.md` contains 65 duplicate action entries.
- `004c8021-6e12-48a0-8948-7155a936b20f.md` turns dialogue about Sweden/Houston
  into a group travel-planning task and assigns an unclear purchase statement to a
  named person.

### Speaker identification

- Production config sets speaker similarity to `0.15`; the speaker service default and
  documented normal threshold is `0.5`.
- Per-segment identification is enabled, allowing different enrolled identities to be
  assigned independently to segments that came from the same provider speaker.
- 7,213 segments received an enrolled identity.
- 4,571/7,213 (63%) are below `0.5`; 1,872 are below `0.3`.
- The gallery contains 43 identities, many backed by a single unverifiable enrollment
  clip.
- 50 conversations have more than six recognized identities.
- Source diarization has a median of three unique speakers. Identification inflated
  identity count in 96/169 conversations.
- `2dbb81a6-126d-477d-a241-f9d4fa9d2f70` expanded from four source speakers to 35
  identities; `0614c7d6-05eb-4327-ac4f-23c7ccddb1e6` expanded from four to 32.
- False identities are propagated into conversation titles, People notes, facts, and
  action ownership. For example, `Amitabh Bachchan` is assigned in 14 conversations,
  often from one segment with confidence around 0.16-0.27.

### Segment duplication

- Memory processing concatenates every active speech segment without overlap or text
  deduplication.
- 101/169 active conversations contain 2,478 adjacent temporal overlaps.
- 82 contain 968 overlapping boundaries with repeated text, totaling 9,835 repeated
  tokens.
- In 16 resegmented conversations, speaker processing increased overlap pairs from 213
  to 412 and repeated-boundary tokens from 717 to 1,986.
- `7620c7b4-c6c4-433d-b2c3-4404ff5346b9` has 37,934 segment words for a 5,828-word
  transcript, a 6.51x expansion. This explains its context overflow and empty memory.

### People and topics

- Only 3/59 People notes and 20/252 Topic notes have valid leading frontmatter.
- 12 People notes are unreferenced by any conversation note.
- 200-210 Topic notes are unreferenced, depending on whether malformed metadata links
  are counted.
- Conversation metadata contains 39 unresolved person links and 33 unresolved topic
  links, largely due to case/name drift.
- Placeholder people exist under multiple variants such as `Speaker 0`, `Speaker 1`,
  `Unknown Speaker 1`, `UnknownSpeaker1`, `Unknown_Speaker_1`, and hyphenated forms.
- Near-duplicate topic families include Esperflow Login, home automation, Formula One,
  wake word, wedding planning, and conversation fragmentation.

### Audio import and ingestion residue

- Whole-conversation decoded-PCM dedupe worked for the archive's 10 duplicate pairs.
- The archive had 234 conversations and 13,494 chunks; the imported database has 224
  conversations. Exactly 10 later duplicate conversations were skipped, retaining the
  first archive version.
- A fresh decoded-PCM scan finds no remaining whole-conversation duplicate group among
  same-structure candidates.
- Separately, the live database has 179 duplicate `(conversation_id, chunk_index)` groups
  containing 183 extra chunk documents across 14 conversations. These are distinct
  compressed bytes and are residue inside retained conversations, not whole-clip
  duplicates.
- `audio_chunks_count` disagrees with the actual chunk count on 19 conversations.
- Current PCM dedupe only compares candidates with identical chunk structure, so
  identical PCM split across different chunk boundaries is not detected.
- Duplicate warnings are logger-only and are not preserved as durable system events.

### Audit ledger

- All 429 update records have a null `before_hash`, breaking update-chain provenance.
- Six current files differ from the latest audited `after_text`, caused by backlink
  rewrites that were not recorded as changes.

## Root Causes

1. `config/config.yml` overrides speaker similarity from the service default `0.5` to
   `0.15` and enables per-segment identity assignment.
2. `workers/memory_jobs.py` concatenates overlapping active segments without
   normalization.
3. The Chronicle provider passes neither source `created_at` nor duration to
   `MemoryAgent.run`, although that method accepts both parameters.
4. `providers/chronicle.py` considers a rebuild successful when the expected path merely
   exists. It does not validate frontmatter, exact ID, source date/duration, required
   sections, placeholders, or substantive content.
5. A general `write_note` tool lets the LLM render schema-critical files as arbitrary
   Markdown. The local 14B model frequently emits malformed tool arguments and ignores
   the template contract; fallback completion does not provide deterministic schema
   enforcement.
6. Import dedupe operates at the conversation level and does not reconcile duplicate
   chunk indices within the retained conversation.

## Required Before Another Rebuild

1. Raise speaker matching to a calibrated open-set threshold and disable per-segment
   identity assignment for rebuilds. Reject ambiguous matches instead of forcing names.
2. Audit/clean the enrollment gallery, especially single-clip and contaminated speakers.
3. Repair duplicate chunk-index residue and count mismatches before reconstructing audio
   or running speaker jobs.
4. Normalize active segments before memory input: stable sort, overlap-aware text
   dedupe, and a bounded context representation.
5. Pass the source date, source title, and measured duration into memory generation.
6. Replace free-form conversation-note writes with a structured tool or deterministic
   renderer.
7. Validate generated notes semantically before marking a job successful. Empty
   templates, placeholder titles, malformed YAML, wrong IDs, wrong metadata, and runaway
   duplicate lines must fail validation.
8. Record import dedupe decisions and every vault mutation durably.
9. Run a small canary rebuild and require the checks in this report to pass before a
   full 169-conversation rebuild.

## Method

Three independent read-only audits inspected structure/source mapping, content fidelity,
and deduplication. Checks used the live MongoDB records, active/source transcript-version
pairs, generated Markdown, audit ledger, speaker-service enrollment health, and decoded
PCM fingerprints. No vault, database, queue, or speaker enrollment data was modified.

## Remediation Result

The clean rebuild completed on 2026-07-16 as run `970899d4dea6`.

- A new 419.9 MiB checksummed archive was created at
  `/app/data/backups/chronicle_20260716_100153.chronicle`; the pre-rebuild vault was
  also saved as `/app/data/backups/memory_vault_20260716_100800.tar.gz`.
- 501 later audio-chunk rows were removed from 485 duplicate
  `(conversation_id, chunk_index)` groups across 33 conversations. The decisions are
  retained in `data_repair_audit`, and the live database now has zero duplicate groups.
- Audio count/duration metadata was recomputed from surviving chunks for 203
  conversations; 13 stale conversation records changed.
- Speaker recognition replayed 178 audio conversations at threshold `0.5` with
  per-segment naming disabled. 177 jobs succeeded; `f2375851-de3d-476e-a5fa-1e15f95ad2bc`
  produced no segments and failed cleanly, leaving its imported source transcript active
  with no partial speaker version. One transcript-only conversation was skipped.
- Memory replay processed 169 eligible conversations with GPT-5.4 mini. It accepted 151
  substantive GPT notes and created 18 source-preserving fallback notes only after both
  GPT attempts returned empty semantic sections.
- Final vault validation found 169 exact-ID conversation files, zero missing or extra
  files, zero ID/date/duration/category errors, zero unknown/Hermes person links, zero
  exact duplicate fact lines, and zero duplicate H2 sections.
- `People/Hermes.md` was merged into `Topics/Hermes.md`; 24 exact repeated fact lines
  were removed from nine structured notes.
- The rebuilt audit ledger has 745 rows. No `update` row has a null `before_hash`.
- Langfuse recorded 2,102,644 uncached input tokens, 11,917,440 cached input tokens,
  206,096 output tokens, and an actual GPT-5.4-mini cost of `$3.398223`.
- Speaker identity remains the non-deterministic limitation: 14 conversations retain
  more than six `Unknown Speaker N` labels because the available audio/gallery evidence
  did not justify assigning names. Those labels were kept unknown rather than guessed.

## Post-Remediation Verification

A second read-only audit of the completed run confirmed that the vault is structurally
sound, but not semantically perfect.

- All 169 eligible conversations have one exact-ID note with parseable trusted metadata,
  substantive Summary and Key Facts sections, and no exact duplicate substantive lines.
- Audio storage has zero duplicate `(conversation_id, chunk_index)` groups, orphan audio
  IDs, count mismatches, duration mismatches, or remaining same-structure PCM duplicate
  groups. Memory-input normalization produces no exact duplicate inputs.
- All current generated Markdown tracked by the audit ledger matches its latest hash.
  Six unaudited Markdown files are expected static hubs/templates. All 745 audit rows have
  intact update chains.
- The focused rebuild regression suite passes: 37 tests.
- Eighteen conversations use deterministic source-excerpt fallbacks. Most sources are
  short or unintelligible, but two fallbacks are materially lossy. One of those sources,
  `31649f4e-df0d-41c6-ad8d-53152cef1690`, is itself corrupted provider/control-token
  output rather than a normal transcript.
- Eight active transcripts contain chat control tokens with serialized segment arrays,
  and eleven contain extreme repeated-word sequences. Some generated memories faithfully
  preserve those bad inputs as if they were real content. These require a transcript
  quality gate and targeted retranscription/rebuild, not another blind full replay.
- Three placeholder action items (`None captured` / `None recorded`) and several vague or
  meta follow-ups remain. Ten of 153 person links are not supported by either an active
  speaker label or a literal name in the transcript and need review.
- Twenty-five Topic notes and two People notes have empty `About` sections. Two Topic
  notes contain a duplicated Conversations base embed. Unresolved topic links are allowed
  breadcrumbs under the current agent contract rather than structural failures.
