# Obsidian companion

Chronicle Companion is a deliberately small Obsidian community plugin for semantic
maintenance of a synced Chronicle vault. It does not run an LLM and does not mutate
Markdown directly. The plugin gathers intent and local file revisions, previews the
operation through Chronicle, and submits the exact approved plan to the backend.

## Person merge

Open a note directly under `People/`, then run **Chronicle: Merge current person…**
from the command palette. Select the canonical person, review the fact/link counts and
metadata conflicts, and confirm the merge. Syncthing delivers the backend-authored files
back to Obsidian.

The backend applies fixed rules:

- retain the target as the canonical note;
- add the source name and aliases to the target aliases;
- union categories, copy identity metadata into empty target fields, and retain target
  values when both sides conflict;
- retain media embeds and merge unique `About`/`Mentions` bullets;
- rewrite direct and `People/`-qualified wikilinks;
- delete the source only after the other writes succeed;
- rollback ordinary write failures and record every changed path under one action ID.

Preview returns a token derived from all affected server-side files. Apply recomputes the
plan under Chronicle's per-user vault lock and rejects a stale token with HTTP 409.
Obsidian also supplies hashes of the local source and target notes, so it receives the
same conflict when Syncthing has not delivered a local edit to the backend yet.

## Duplicate review

Run **Chronicle: Review possible duplicate people…** from the command palette. Chronicle
uses conservative deterministic signals—name/alias similarity or a shared photo—and may
add shared conversation, link, organization, or role context to rank the candidates.
Context alone never creates a suggestion, and no suggestion is merged automatically.

Each card supports three outcomes:

- **Same person…** selects the canonical name and opens the ordinary merge preview;
- **Separate people** writes symmetric `distinct_from` wikilinks into both People notes,
  audits the changes, suppresses the candidate, and blocks an accidental later merge;
- **Not sure** hides that exact candidate revision only in the local plugin settings. A
  change to either note gives the candidate a new revision, allowing it to surface again.

The backend rejects a stale separate-person decision if either note changed after the
suggestion was shown.

## Configuration

Build from `extras/obsidian-chronicle/` with `npm install && npm run build`. Install
`main.js`, `manifest.json`, and `styles.css` under
`.obsidian/plugins/chronicle-companion/`.

In Obsidian settings, configure the Chronicle HTTPS address and select a long-lived,
revocable Chronicle API key through Obsidian SecretStorage. The plugin stores only the
secret's name in its own `data.json`.

## Automation skill

The shared `chronicle-merge-person` Agent Skill uses the same suggestion, identity, and
merge endpoints. It never edits vault files itself. This keeps identity discovery
optionally agentic while keeping every mutation deterministic.
