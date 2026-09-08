# Recording coverage and activity rejection

## Intent

A recorder running is not evidence that a person performed an activity. Recorded silence, no qualifying speech, and uncovered source timestamps are distinct. Pausing playback or speech is not missing coverage when capture continues.

## Automatic rule

`activity_policy.py` owns the rule used by day rendering, semantic-memory eligibility, grouping and reconciliation publication. An episode supported entirely by capture gaps and speech-free audio spans is coverage-only when measured acoustic activity is at most 0.1% of the span. Explicit human title/type labels preserve an intentional activity. Transcribed, unscored or substantially audible spans remain retained; review eligibility is checked separately below.

Reconciliation cannot publish coverage-only model output and retires coverage-only priors through its normal journal. Existing snapshots can apply exactly the same rule with `retire_recording_only_episodes`. Recordings, audio chunks, evidence spans and prior episode revisions remain intact. Historical day rendering excludes these entries from activity confirmation and shows no-speech spans separately on the coverage tape.

## Review eligibility

A separate shared gate, `episode_requires_activity_review`, keeps media dialogue, empty uncertain transcripts, and recorder metadata out of human activity confirmation, new grouping proposals, and automatic semantic memory. Generated titles, types, foreground labels, or confidence cannot override it. These retained episodes project into the background lane; raw evidence and prior model output remain inspectable. This gate is evaluated on every request, so existing snapshots benefit without regeneration or ad hoc deletion. Cached grouping proposals containing a reference-only member are invalidated as a whole: their generated rationale cannot be reused for a silently trimmed membership. They are hidden from review, cannot be accepted through a stale browser, and do not block finalization. Accepted historical groups remain intact.

Any screen/photo observation or meaningful non-media transcript preserves eligibility, including activity on another device. Explicit human title/type edits and Remember content preserve user intent. Evidence-free manual episodes are not automatically suppressed. Coverage-only retirement remains the narrower quiet-recording rule above.

The API returns `requires_activity_review` and `memory_eligible`; the frontend consumes these decisions instead of duplicating model/type heuristics. Reference-only members of a mixed session are marked Reference only, never Already reviewed. They cannot be submitted for confirmation and do not prevent finalizing the real activities. A Not an activity decision remains a separate explicit rejection.

## Not an activity

A snapshot/revision-fenced API records `episode_not_activity` through the existing manual publication journal. It removes only the selected episode claim and preserves any accepted group with at least two remaining members. The decision stores the rejected bounds and evidence identities/content hashes. Reconciliation suppresses repeated claims based on the same evidence within those bounds, regardless of generated title or episode ID. New or changed substantive evidence can support another proposal; extra recording-gap metadata cannot bypass a rejection of the same substantive evidence. Other tracks and unrelated overlapping activities remain eligible. Publication uses the same day snapshot versions read with these decisions, so concurrent review fences stale work.

The session review action opens an inline explanation and second-click confirmation naming the selected episode. Later remains a navigation action without any saved rejection. No action removes raw recordings or starts memory extraction.

## Gap presentation and model input

Quiet-source intervals appear as recording coverage with their sound-activity and uncovered-time measurements. A gap count over a source span must not be drawn or used as an exact missing interval: its precise positions were not stored. Such aggregate gaps have no exact boundary anchors. Model compaction preserves `no_speech`, covered/missing seconds and acoustic measurements. UI copy distinguishes recording from playback, and unidentified speaker placeholders do not become group participants.

## Validation

Exercise real publication and API entry points for removal, stale rejection, regeneration suppression, unrelated/new evidence, and accepted-group preservation. Test continuous quiet coverage versus missing timestamps and retention of actual speech/sound. Verify desktop/phone light/dark screenshots with intercepted review writes.
