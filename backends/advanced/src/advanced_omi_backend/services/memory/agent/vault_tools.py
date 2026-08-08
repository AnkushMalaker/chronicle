"""Vault tools for the Chronicle memory agent.

A filesystem-scoped toolset the agent calls to maintain a per-user Obsidian-style
markdown vault:

    {vault_root}/Conversations/<conversation_id>.md
    {vault_root}/People/<name>.md
    {vault_root}/Topics/<topic>.md

Search is modelled on Claude Code: a ripgrep-backed ``grep`` (full regex, glob filter,
output modes) and a ``glob`` (filename patterns) that the LLM drives by formulating
patterns — no query preprocessing. Editing uses exact string-replace (see
:mod:`edit_engine`). ``rename_person`` renames a person note and rewrites every
``[[wikilink]]`` across the vault (via notesmd-cli ``move`` when present, else Python).

Frontmatter is edited as text via ``edit_note`` — never through notesmd-cli's
``frontmatter --edit``, which corrupts wikilinks and list/number values.
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

from ..person_merge import PersonMergeService
from ..telemetry import (
    current_memory_attempt,
    memory_span,
    set_observation_io,
    set_safe_span_attributes,
    text_payload,
)
from ..vault_lock import VaultLockTimeout, vault_note_lock
from ..vault_scaffold import (
    VaultPathError,
    confined_vault_path,
    safe_vault_relative_path,
    validate_category_name,
    write_category,
)
from ..vault_verify import (
    Finding,
    new_duplicate_sections,
    new_note_schema_problems,
    render_findings,
    section_counts,
    verify_vault_changes,
)
from .edit_engine import Edit, EditError, apply_edits
from .section_edit import SectionEditError, apply_section_edit

logger = logging.getLogger("memory_service.agent.tools")

_GREP_MAX_LINES = 200  # default head limit, like Claude Code's grep
# A note has no bound on its length, so a read of one needs its own. Sized so a full
# window is a few thousand tokens: enough to work with, small enough that several reads
# still leave room for the transcript in a 32k-64k local context.
_READ_DEFAULT_LINES = 400
_READ_MAX_LINES = 2000
_READ_MAX_CHARS = 20000


class VaultToolError(Exception):
    """Tool-level failure. The message is returned to the model so it can retry."""


def _safe_relpath(path: str) -> str:
    """Reject absolute paths, traversal, and slash-in-title nesting; normalise to a
    vault-relative ``<Folder>/<Title>.md`` (or top-level hub ``<Title>.md``) path.

    Whitespace around components is stripped — a title like ``"TailScale "`` would
    otherwise mint a trailing-space file/folder name that breaks Windows and Syncthing.
    """
    if not isinstance(path, str):
        raise VaultToolError("Note paths must be strings.")
    p = path.strip()
    if not p.endswith(".md"):
        p += ".md"
    try:
        safe = safe_vault_relative_path(p)
    except VaultPathError as exc:
        raise VaultToolError(
            f"Invalid path '{path}': must stay inside the vault."
        ) from exc
    parts = list(Path(safe).parts)
    if len(parts) > 2:
        raise VaultToolError(
            f"Invalid path '{path}': notes live at <Folder>/<Title>.md, one folder "
            f"deep. A '/' in a note title would create nested folders — rephrase the "
            f"title without '/' (e.g. 'Tailscale VPN WireGuard', not "
            f"'TailScale / VPN / WireGuard')."
        )
    dirs = [d.strip() for d in parts[:-1]]
    stem = parts[-1][: -len(".md")].strip()
    if not stem or any(not d for d in dirs):
        raise VaultToolError(f"Invalid path '{path}': empty folder or note title.")
    normalized = str(Path(*dirs, stem + ".md"))
    try:
        return safe_vault_relative_path(normalized)
    except VaultPathError as exc:
        raise VaultToolError(
            f"Invalid path '{path}': must stay inside the vault."
        ) from exc


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# The rules themselves live in ..vault_verify so the same logic can run both here (at
# the mutation boundary) and afterwards over a whole-vault diff.
_section_counts = section_counts


# Sections whose bullets carry accumulated facts and must survive a person-note
# merge. ``Conversations`` is intentionally excluded — it holds a dynamic
# ``![[Conversations.base]]`` embed, not facts to migrate.
_MERGE_SECTIONS = ("About", "Mentions")


def _extract_section_bullets(content: str, heading: str) -> List[str]:
    """Return the non-empty bullet lines under a ``## heading`` (case-insensitive).

    A bare ``-`` placeholder (an empty template bullet) is skipped so migrating an
    untouched section contributes nothing.
    """
    want = heading.casefold()
    out: List[str] = []
    in_section = False
    for line in content.splitlines():
        m = _H2_RE.match(line.rstrip())
        if m:
            in_section = m.group(1).casefold() == want
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("-") and stripped.lstrip("-").strip():
                out.append(line.rstrip())
    return out


def _assert_no_new_section_dupes(rel: str, before: str, after: str) -> None:
    """Reject a mutation that *introduces* a duplicated ``## Section`` heading.

    A structured note (person/topic/etc.) carries each section — ``## About``,
    ``## Mentions``, … — exactly once. The classic agent failure is re-emitting the
    whole note template (or re-pasting a section) so the body stacks 2–3×. This is
    a deterministic backstop at the tool boundary: it blocks the corrupt write
    regardless of how the model misbehaved, and surfaces a corrective message.

    We compare against ``before`` and only reject *new* duplication, so an edit to
    an already-duplicated note can still proceed (e.g. while repairing it).
    """
    offenders = new_duplicate_sections(before, after)
    if offenders:
        ac = _section_counts(after)
        pretty = ", ".join(f"'## {h}' (×{ac[h]})" for h in offenders)
        raise VaultToolError(
            f"Refusing to write '{rel}': it would duplicate section heading(s) "
            f"{pretty}. A note must contain each section once. Do NOT re-emit the "
            f"note template or paste a whole section into an existing note — "
            f"read_note it and edit_note only the genuinely new lines into the "
            f"section that already exists."
        )


def _assert_new_note_schema(rel: str, content: str) -> None:
    """Reject incomplete new People/Topic notes at the mutation boundary.

    Local models sometimes emit only ``## About`` even though the canonical template
    is in their prompt. Accepting that write leaves a permanently malformed note and
    gives the agent no signal to repair it. A tool error is recoverable in the same
    agent turn, so require the stable spine sections and aggregation embed up front.
    """
    problems = new_note_schema_problems(rel, content)
    if not problems:
        return
    template_name = "Person" if Path(rel).parts[0] == "People" else "Topic"
    raise VaultToolError(
        f"Refusing to create '{rel}': {'; '.join(problems)}. Read and fill the "
        f"canonical Templates/{template_name} Template.md, preserving every required "
        "section and embed."
    )


class VaultTools:
    """Filesystem-scoped tool implementations for one user's vault."""

    def __init__(
        self,
        vault_root: Path,
        *,
        trace_context: Any = None,
        required_notes: Sequence[str] = (),
        forbidden_folders: Sequence[str] = (),
    ):
        # Notes this run must create or edit; verify_vault reports any that it has not.
        self.required_notes = tuple(required_notes)
        # Folders this run must not touch at all (a day write must not mint a
        # Conversations/ note, which is keyed by a conversation_id it does not have).
        self.forbidden_folders = tuple(forbidden_folders)
        self.root = Path(vault_root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise VaultToolError(
                f"Vault root must not be a symbolic link: {self.root}."
            )
        self._resolved_root = self.root.resolve(strict=True)
        self._rg = shutil.which("rg")
        self._notesmd = os.getenv("NOTESMD_CLI_BIN") or shutil.which("notesmd-cli")
        # Pi dispatches through a loopback HTTP server on fresh request threads.
        # Retaining the caller's immutable OTEL context keeps those tool spans under
        # the Pi agent rather than creating unrelated root traces. Context variables
        # do not cross those request threads, so retain the attempt label as well.
        self._trace_context = trace_context
        self._trace_attempt = current_memory_attempt()
        self.touched: set = set()  # vault-relative paths created/edited this run
        self.verified = False  # whether the agent called verify_vault before finishing
        # Unlike ``touched``, this is monotonic: editing the same note twice must
        # still mark both tool observations as mutating.
        self._mutation_count = 0
        # Notes retired by a rename/merge this run. Each entry is
        # {"old_path", "new_path", "before"} — the audit step turns these into
        # ``rename`` ledger entries so a note vanishing is never invisible.
        self.removed: List[dict] = []
        # Vault contents as this run found them. ``verify_vault`` diffs against it, so
        # the agent is only ever shown problems it introduced. Captured lazily: a
        # search-only run never pays for it.
        self._baseline: Dict[str, str] | None = None

    def baseline(self) -> Dict[str, str]:
        """Snapshot of the vault at the start of this run, taken once."""

        if self._baseline is None:
            self._baseline = {
                rel: path.read_text(encoding="utf-8", errors="replace")
                for rel, path in (
                    (p.relative_to(self.root).as_posix(), p) for p in self._all_md()
                )
            }
        return self._baseline

    def verify_vault(self) -> str:
        """Report this run's vault problems, phrased so the agent can fix them.

        A required note is only demanded once the run has edited something. An agent
        that touched nothing has judged the source already covered, which is a
        legitimate outcome; demanding the record note there would turn a correct no-op
        into a redundant write.
        """

        self.verified = True
        return render_findings(
            verify_vault_changes(
                self.root,
                self.baseline(),
                required=self.required_notes if self.touched else (),
                forbidden_folders=self.forbidden_folders,
            )
        )

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise ONE mutation against concurrent writers of this user's vault.

        The vault root's directory name is the user id (``conversation_docs/<user_id>``).
        Every mutating tool runs its full resolve-check-write sequence inside this lock
        so exists/case-collision checks cannot race a concurrent agent's write.
        """
        # Every mutator passes through here before touching a file, so this is where
        # "the vault as this run found it" is still true. Capturing it lazily inside
        # verify_vault instead would snapshot the agent's own writes and report clean.
        # Taken outside the lock: it is pure reads and can be slow on a large vault.
        self.baseline()
        try:
            with vault_note_lock(self.root.name):
                yield
        except VaultLockTimeout:
            raise VaultToolError(
                "The vault is briefly locked by another writer. Retry this exact "
                "operation; if editing, read_note the file again first — it may have "
                "changed."
            )

    # --- path helpers -------------------------------------------------------

    def _assert_root_safe(self) -> None:
        """Fail if the vault root was replaced or redirected after construction."""
        if (
            self.root.is_symlink()
            or not self.root.is_dir()
            or self.root.resolve(strict=True) != self._resolved_root
        ):
            raise VaultToolError("Vault root changed or became a symbolic link.")

    def _confined_path(self, rel: str) -> Path:
        self._assert_root_safe()
        try:
            return confined_vault_path(self.root, rel)
        except VaultPathError as exc:
            raise VaultToolError(str(exc)) from exc

    def _safe_audit_path(self, rel: str, *, require_exists: bool) -> str:
        """Validate a path before it enters ``touched``/``removed`` audit data."""
        try:
            safe = safe_vault_relative_path(rel)
        except VaultPathError as exc:
            raise VaultToolError(f"Unsafe vault audit path {rel!r}: {exc}") from exc
        target = self._confined_path(safe)
        if require_exists and not target.is_file():
            raise VaultToolError(
                f"Vault audit path does not name a regular file: {safe!r}."
            )
        return Path(safe).as_posix()

    def _mark_touched(self, rel: str) -> None:
        self.touched.add(self._safe_audit_path(rel, require_exists=True))
        self._mutation_count += 1

    def _resolve_ci(self, rel: str) -> str:
        """Map a vault-relative path onto an existing file matching case-insensitively.

        The backend filesystem is case-sensitive, but macOS/Windows clients are not.
        Writing ``People/Hermes.md`` when ``People/hermes.md`` already exists would
        create a case-variant sibling here that collides irrecoverably once Syncthing
        pushes both to a case-insensitive client. Resolving each path component
        against what is already on disk makes the agent reuse the existing note
        instead, so two notes never differ only by case.
        """
        self._assert_root_safe()
        current = self.root
        resolved: List[str] = []
        parts = Path(rel).parts
        for i, part in enumerate(parts):
            exact = current / part
            if exact.is_symlink():
                raise VaultToolError(
                    f"Vault path must not traverse a symbolic link: {rel!r}."
                )
            if exact.exists():
                current = exact
                resolved.append(part)
                continue
            match = None
            if current.is_dir():
                lowered = part.lower()
                match = next(
                    (e.name for e in current.iterdir() if e.name.lower() == lowered),
                    None,
                )
            if match:
                current = current / match
                if current.is_symlink():
                    raise VaultToolError(
                        f"Vault path must not traverse a symbolic link: {rel!r}."
                    )
                resolved.append(match)
            else:
                resolved.extend(parts[i:])  # no match: keep requested casing onward
                break
        return str(Path(*resolved)) if resolved else rel

    def _abs(self, path: str) -> Path:
        rel = self._resolve_ci(_safe_relpath(path))
        return self._confined_path(rel)

    def _all_md(self) -> List[Path]:
        """Return regular Markdown files, rejecting any symlink in the vault tree."""
        self._assert_root_safe()
        paths: List[Path] = []
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                raise VaultToolError(
                    f"Vault contains a symbolic link and cannot be mutated safely: {rel!r}."
                )
            self._confined_path(rel)
            if path.is_file() and path.suffix == ".md":
                paths.append(path)
        return paths

    def _markdown_hashes(self) -> Dict[str, str]:
        """Fingerprint all safe Markdown files for external-tool audit diffs."""
        hashes: Dict[str, str] = {}
        for path in self._all_md():
            rel = self._safe_audit_path(
                path.relative_to(self.root).as_posix(), require_exists=True
            )
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    # --- search (ripgrep) ---------------------------------------------------

    def grep(
        self,
        pattern: str,
        glob: str = "",
        output_mode: str = "files_with_matches",
        ignore_case: bool = True,
        context: int = 0,
        head_limit: int = _GREP_MAX_LINES,
    ) -> str:
        """ripgrep over the vault. Returns text (paths / `path:line:text` / `path:count`)."""
        self._assert_root_safe()
        if not self._rg:
            raise VaultToolError(
                "ripgrep (rg) is not installed in this environment; cannot search."
            )
        args = [self._rg, "--no-messages", "--color=never"]
        if ignore_case:
            args.append("-i")
        if glob:
            args += ["--glob", glob]
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        elif output_mode == "content":
            args.append("-n")
            if context:
                args += ["-C", str(int(context))]
        else:
            raise VaultToolError(
                f"Invalid output_mode '{output_mode}': use files_with_matches, content, or count."
            )
        # Explicit "." path is REQUIRED: with no path and a non-TTY stdin (always the
        # case under subprocess), ripgrep searches stdin instead of the directory.
        args += ["--", pattern, "."]
        try:
            proc = subprocess.run(
                args, cwd=self.root, capture_output=True, text=True, timeout=20
            )
        except subprocess.TimeoutExpired:
            raise VaultToolError(f"Search timed out for pattern {pattern!r}.")
        # rg exit code: 0 = matches, 1 = no matches (not an error), 2 = real error
        if proc.returncode == 2:
            raise VaultToolError(
                f"Invalid regex {pattern!r}: {proc.stderr.strip() or 'ripgrep error'}"
            )
        out = proc.stdout.strip()
        if not out:
            return "No matches found."
        # Strip the leading "./" ripgrep adds when searching ".".
        lines = [ln[2:] if ln.startswith("./") else ln for ln in out.splitlines()]
        if len(lines) > head_limit:
            extra = len(lines) - head_limit
            lines = lines[:head_limit]
            lines.append(f"... ({extra} more line(s) truncated; refine the pattern)")
        return "\n".join(lines)

    def glob(self, pattern: str) -> str:
        """Find notes by filename pattern (e.g. ``People/*.md``). Returns paths."""
        self._assert_root_safe()
        if self._rg:
            proc = subprocess.run(
                [self._rg, "--files", "--glob", pattern],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=20,
            )
            out = proc.stdout.strip()
        else:
            out = "\n".join(
                str(p.relative_to(self.root))
                for p in self._all_md()
                if Path(str(p.relative_to(self.root))).match(pattern)
            )
        return out or "No files found."

    # --- read / write -------------------------------------------------------

    def read_note(
        self, path: str, offset: int = 0, limit: int = _READ_DEFAULT_LINES
    ) -> str:
        """Read a window of a note, numbered from ``offset``.

        Returning whole files is what broke the settled-day write: ``Daily/<date>.md``
        had grown to 215 KB (~55k tokens) and one call consumed 84% of a 65k context,
        so every attempt failed on context size before it could edit anything. A note
        has no bound on its length, so a read of one must have its own.

        Windowed rather than simply truncated: the agent can page to the part it needs.
        Most of the time it needs none of this — ``edit_section`` appends by heading
        without reading the note at all.
        """
        fp = self._abs(path)
        if not fp.exists():
            raise VaultToolError(
                f"Note '{path}' does not exist. Use glob or grep to find the right "
                f"path, or write_note to create it."
            )
        try:
            offset = max(0, int(offset))
            limit = int(limit)
        except (TypeError, ValueError):
            raise VaultToolError("read_note offset and limit must be integers.")
        if limit <= 0:
            limit = _READ_DEFAULT_LINES
        limit = min(limit, _READ_MAX_LINES)

        # keepends, so an unwindowed read returns the file byte-for-byte — edit_note
        # matches old_text exactly, and a silently dropped trailing newline would make
        # an edit copied from a read fail to apply.
        lines = fp.read_text(encoding="utf-8").splitlines(keepends=True)
        total = len(lines)
        window = lines[offset : offset + limit]

        # A single very long line can still blow the window, so cap the characters too.
        body = "".join(window)
        char_capped = False
        if len(body) > _READ_MAX_CHARS:
            body = body[:_READ_MAX_CHARS]
            char_capped = True

        shown_to = offset + len(window)
        if offset == 0 and shown_to >= total and not char_capped:
            return body
        notes = [f"[showing lines {offset + 1}-{shown_to} of {total}]"]
        if char_capped:
            notes.append(f"[truncated at {_READ_MAX_CHARS} characters]")
        if shown_to < total:
            notes.append(
                f"[continue with read_note(path, offset={shown_to}) — or prefer "
                f"grep to find the part you need, and edit_section to append without "
                f"reading the whole note]"
            )
        return f"{body}\n\n" + "\n".join(notes)

    def edit_note(self, path: str, edits: List[Dict[str, str]]) -> str:
        parsed = [Edit(e["old_text"], e["new_text"]) for e in edits]
        with self._locked():
            fp = self._abs(path)
            if not fp.exists():
                raise VaultToolError(
                    f"Could not edit '{path}': file not found. Use write_note to create it."
                )
            content = fp.read_text(encoding="utf-8")
            try:
                new_content = apply_edits(content, parsed, path)
            except EditError as e:
                raise VaultToolError(str(e))
            _assert_no_new_section_dupes(path, content, new_content)
            fp.write_text(new_content, encoding="utf-8")
            self._mark_touched(self._resolve_ci(_safe_relpath(path)))
        return f"Edited {path} ({len(edits)} replacement(s))."

    def edit_section(
        self, path: str, target: str, text: str, operation: str = "append"
    ) -> str:
        """Structurally targeted edit: append/prepend/replace under a ``## Heading`` or
        ``^block-ref`` — without needing the section's current text as an anchor.

        Unlike ``edit_note`` (which matches a literal ``old_text`` slice and so fails
        when a concurrent writer has changed that slice), this targets the note's
        structure, so the common "append a new fact under ## About" survives concurrent
        edits that don't restructure the note.
        """
        with self._locked():
            fp = self._abs(path)
            if not fp.exists():
                raise VaultToolError(
                    f"Could not edit '{path}': file not found. Use write_note to create it."
                )
            content = fp.read_text(encoding="utf-8")
            try:
                new_content = apply_section_edit(content, target, text, operation)
            except SectionEditError as e:
                raise VaultToolError(str(e))
            _assert_no_new_section_dupes(path, content, new_content)
            fp.write_text(new_content, encoding="utf-8")
            self._mark_touched(self._resolve_ci(_safe_relpath(path)))
        return f"Edited {path} ({operation} under '{target}')."

    def write_note(self, path: str, content: str, overwrite: bool = False) -> str:
        with self._locked():
            rel = self._resolve_ci(_safe_relpath(path))
            fp = self._confined_path(rel)
            if fp.exists() and not overwrite:
                raise VaultToolError(
                    f"Note '{rel}' already exists. Use edit_note to modify it, or pass "
                    f"overwrite=true only if you intend to replace it entirely."
                )
            existed = fp.exists()
            # Long-lived structured notes are maintained incrementally — overwriting
            # one wholesale either duplicates the body or drops accumulated facts.
            # Force those updates through edit_note.
            top_folder = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""
            note_stem = Path(rel).stem
            if top_folder == "People" and (
                re.fullmatch(r"unknown speaker(?:\s+\d+)?", note_stem, re.IGNORECASE)
                or note_stem.casefold() == "hermes"
            ):
                raise VaultToolError(
                    "Unknown Speaker diarization placeholders and the Hermes assistant are not people; "
                    "do not create or link a person note for them. Use Topics/Hermes.md "
                    "for the Hermes assistant."
                )
            if existed and overwrite and top_folder in ("People", "Topics"):
                raise VaultToolError(
                    f"Refusing to overwrite existing note '{rel}'. People/Topics notes "
                    f"accumulate facts over time — never replace them wholesale. "
                    f"read_note it and edit_note only the new lines."
                )
            before = fp.read_text(encoding="utf-8") if existed else ""
            _assert_no_new_section_dupes(rel, before, content)
            if not existed:
                _assert_new_note_schema(rel, content)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp = self._confined_path(rel)
            fp.write_text(content, encoding="utf-8")
            self._mark_touched(rel)
        return f"{'Overwrote' if existed else 'Wrote'} {rel} ({len(content)} chars)."

    def create_category(self, name: str, properties: List[str] | None = None) -> str:
        """Mint a new organic category: its template, base, and hub note (idempotent).

        ``name`` should be the plural category name (e.g. ``"Places"``); ``properties`` the
        short, reusable frontmatter keys its notes carry (e.g. ``["location", "type"]``).
        """
        try:
            category = validate_category_name(name)
        except VaultPathError as exc:
            raise VaultToolError(f"Invalid category name {name!r}: {exc}") from exc
        with self._locked():
            try:
                created = write_category(self.root, category, properties or [])
            except VaultPathError as exc:
                raise VaultToolError(str(exc)) from exc
            for rel in created:
                self._mark_touched(rel)
        if created:
            return (
                f"Created category '{category}' ({', '.join(created)}). Now file notes under "
                f'{category}/<Title>.md with categories: ["[[{category}]]"], using '
                f"Templates/{category} Template.md as the shape."
            )
        return (
            f"Category '{category}' already exists. File notes under "
            f"{category}/<Title>.md and read Templates/{category} Template.md for its schema."
        )

    def rename_person(self, old_name: str, new_name: str) -> str:
        old_rel, new_rel = f"People/{old_name}.md", f"People/{new_name}.md"
        with self._locked():
            old_fp, new_fp = self._abs(old_rel), self._abs(new_rel)
            if not old_fp.exists():
                raise VaultToolError(
                    f"Person note 'People/{old_name}.md' does not exist."
                )
            # Both merge implementations scan and rewrite backlinks across the
            # complete vault. Reject a symlink anywhere before handing paths to
            # PersonMergeService or notesmd-cli, neither of which is a confinement
            # boundary on its own.
            self._all_md()
            # Snapshot the retiring note before it moves/unlinks so the audit ledger
            # keeps its final content and the merge never loses facts unrecorded.
            old_content = old_fp.read_text(encoding="utf-8")
            if new_fp.exists():
                # Merge case — delegate to the same deterministic operation exposed to
                # Obsidian and automation clients. The caller already owns the vault
                # lock, so use its locked implementation directly.
                service = PersonMergeService(self.root)
                preview = service.preview(old_name, new_name)
                result = service.apply_preview_locked(preview)
                for rel, after in result.after.items():
                    if after is not None:
                        self._mark_touched(rel)
                self._record_removal(old_rel, new_rel, old_content)
                return (
                    f"'{new_name}' already existed — merged into People/{new_name}.md: "
                    f"migrated {preview.facts_to_add} fact bullet(s), skipped "
                    f"{preview.duplicate_facts_skipped} duplicate(s), rewrote "
                    f"{preview.backlink_occurrences} backlink(s), added '{old_name}' as "
                    f"an alias, and deleted People/{old_name}.md."
                )
            if self._notesmd:
                before_cli = self._markdown_hashes()
                try:
                    self._move_cli(old_rel, new_rel)
                except Exception as e:  # noqa: BLE001
                    logger.warning("notesmd-cli move failed (%s); using python", e)
                    # A failed external command may still have rewritten some
                    # backlinks before returning non-zero. Snapshot again before
                    # deciding whether the Python fallback is safe, and retain
                    # those mutations in the audit set: the fallback will see the
                    # already-rewritten text as unchanged and cannot rediscover it.
                    after_failed_cli = self._markdown_hashes()
                    if not old_fp.is_file() or new_fp.exists():
                        raise VaultToolError(
                            "notesmd-cli failed after partially moving the person note; "
                            "refusing a second rename"
                        ) from e
                    for rel, digest in after_failed_cli.items():
                        if before_cli.get(rel) != digest:
                            self._mark_touched(rel)
                else:
                    # Once the external move succeeds, all validation/audit failures
                    # fail closed; retrying a Python rename against an already-moved
                    # source would compound a partial result.
                    after_cli = self._markdown_hashes()
                    for rel, digest in after_cli.items():
                        if before_cli.get(rel) != digest:
                            self._mark_touched(rel)
                    self._record_removal(old_rel, new_rel, old_content)
                    return f"Renamed People/{old_name} -> People/{new_name} (backlinks rewritten)."
            n = self._rewrite_backlinks_python(old_name, new_name)
            old_fp.rename(new_fp)
            self._mark_touched(new_rel)
            self._record_removal(old_rel, new_rel, old_content)
        return f"Renamed People/{old_name} -> People/{new_name} ({n} backlink(s) rewritten)."

    def _record_removal(self, old_rel: str, new_rel: str, before: str) -> None:
        """Queue a rename/merge removal for the audit ledger and clear any prior
        ``touched`` entry for the vanished path (it no longer exists to re-read)."""
        old_safe = self._safe_audit_path(old_rel, require_exists=False)
        new_safe = self._safe_audit_path(new_rel, require_exists=True)
        self.touched.discard(old_safe)
        self.removed.append(
            {"old_path": old_safe, "new_path": new_safe, "before": before}
        )

    def _migrate_person_facts(
        self, old_content: str, new_fp: Path, old_rel: str
    ) -> int:
        """Append the retiring note's fact bullets into the merge target so a merge
        never silently drops accumulated facts. Bullets land under the matching
        ``## About`` / ``## Mentions`` heading; if the target lacks a heading, they go
        into a ``## Merged from <old>`` section rather than being lost. Duplicates are
        acceptable here — the agent de-duplicates afterward. Returns bullets migrated.
        """
        target = new_fp.read_text(encoding="utf-8")
        migrated = 0
        orphaned: List[str] = []
        for heading in _MERGE_SECTIONS:
            bullets = _extract_section_bullets(old_content, heading)
            if not bullets:
                continue
            block = "\n".join(bullets)
            try:
                target = apply_section_edit(target, heading, block, "append")
            except SectionEditError:
                orphaned.extend(bullets)
            migrated += len(bullets)
        if orphaned:
            target = (
                target.rstrip("\n")
                + f"\n\n## Merged from {old_rel}\n"
                + "\n".join(orphaned)
                + "\n"
            )
        if migrated:
            new_fp.write_text(target, encoding="utf-8")
        return migrated

    def _move_cli(self, old_rel: str, new_rel: str) -> None:
        assert self._notesmd is not None  # callers gate on truthiness
        subprocess.run(
            [
                self._notesmd,
                "move",
                old_rel[:-3],
                new_rel[:-3],
                "--vault",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def _rewrite_backlinks_python(self, old_name: str, new_name: str) -> int:
        """Replace ``[[old_name]]`` / ``[[old_name|...]]`` / ``[[old_name#...]]`` everywhere."""
        changed = 0
        for fp in self._all_md():
            text = fp.read_text(encoding="utf-8")
            new = (
                text.replace(f"[[{old_name}]]", f"[[{new_name}]]")
                .replace(f"[[{old_name}|", f"[[{new_name}|")
                .replace(f"[[{old_name}#", f"[[{new_name}#")
            )
            if new != text:
                fp.write_text(new, encoding="utf-8")
                self._mark_touched(fp.relative_to(self.root).as_posix())
                changed += 1
        return changed

    # --- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, args: Dict[str, Any]) -> str:
        """Run a tool by name; always returns a string for the tool message."""
        serialized_args = json.dumps(
            args, ensure_ascii=False, sort_keys=True, default=str
        )
        mutations_before = self._mutation_count
        with memory_span(
            f"memory_tool.{name}",
            parent_context=self._trace_context,
            attributes={
                "openinference.span.kind": "TOOL",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "chronicle.memory.tool.name": name,
                "chronicle.memory.tool.argument_keys": sorted(str(key) for key in args),
                "chronicle.memory.attempt": self._trace_attempt,
            },
        ) as span:
            set_observation_io(
                span,
                input={"arguments": text_payload(serialized_args)},
            )
            result = self._dispatch(name, args)
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": True,
                    "chronicle.memory.tool.result_chars": len(result),
                    "chronicle.memory.tool.mutated": self._mutation_count
                    > mutations_before,
                    "chronicle.memory.tool.mutation_count": self._mutation_count
                    - mutations_before,
                },
            )
            set_observation_io(span, output={"result": text_payload(result)})
            return result

    def _dispatch(self, name: str, args: Dict[str, Any]) -> str:
        """Untraced canonical dispatch; :meth:`dispatch` owns the observation."""
        if name == "grep":
            return self.grep(
                args["pattern"],
                glob=args.get("glob", ""),
                output_mode=args.get("output_mode", "files_with_matches"),
                ignore_case=args.get("ignore_case", True),
                context=args.get("context", 0),
                head_limit=args.get("head_limit", _GREP_MAX_LINES),
            )
        if name == "glob":
            return self.glob(args["pattern"])
        if name == "read_note":
            return self.read_note(
                args["path"],
                offset=args.get("offset", 0),
                limit=args.get("limit", _READ_DEFAULT_LINES),
            )
        if name == "edit_note":
            return self.edit_note(args["path"], args["edits"])
        if name == "edit_section":
            return self.edit_section(
                args["path"],
                args["target"],
                args["text"],
                args.get("operation", "append"),
            )
        if name == "write_note":
            return self.write_note(
                args["path"], args["content"], args.get("overwrite", False)
            )
        if name == "rename_person":
            return self.rename_person(args["old_name"], args["new_name"])
        if name == "create_category":
            return self.create_category(args["name"], args.get("properties"))
        if name == "verify_vault":
            return self.verify_vault()
        raise VaultToolError(f"Unknown tool: {name}")


# --- OpenAI function-calling schemas ----------------------------------------

_GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search note CONTENTS with a regular expression (ripgrep). Use this FIRST "
            "to find where a person/topic/fact is mentioned before reading or creating "
            "notes.\n"
            "- `pattern` is a full regex, e.g. `3D\\s*print`, `[Hh]inglish`, "
            "`Goroku|Heroku`. Case-insensitive by default.\n"
            "- Filter files with `glob`, e.g. `People/*.md`, `Conversations/*.md`.\n"
            "- `output_mode`: 'files_with_matches' (default, just paths), 'content' "
            "(matching lines with line numbers; supports `context`), or 'count'.\n"
            "- Prefer a tight pattern over reading whole notes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex to search for."},
                "glob": {
                    "type": "string",
                    "description": "Optional filename filter, e.g. 'People/*.md'.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                },
                "ignore_case": {"type": "boolean"},
                "context": {
                    "type": "integer",
                    "description": "Lines of context around each match (content mode).",
                },
                "head_limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
}

_GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": (
            "Find notes by FILENAME pattern (e.g. 'People/*.md', '**/*ankush*'). "
            "Returns matching paths. Use to locate a person/topic note before reading "
            "it, or to list a folder."
        ),
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
}

_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_note",
        "description": (
            "Read a window of a note by vault-relative path (e.g. 'People/Alice.md'). "
            f"Returns up to {_READ_DEFAULT_LINES} lines from `offset`; long notes are "
            "reported with their total length and how to page on. To ADD a fact you do "
            "not need to read the note at all — `edit_section` appends by heading. Use "
            "`grep` to locate the part you care about rather than paging a long note."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "description": "0-based first line to return (default 0).",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Lines to return (default {_READ_DEFAULT_LINES}, max "
                        f"{_READ_MAX_LINES})."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}

_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_note",
        "description": "Surgical mid-line / frontmatter edits via exact string "
        "replacement. Each edit's old_text must match the current file EXACTLY "
        "(whitespace included) and be UNIQUE — include surrounding context. Use this for "
        "frontmatter and for fixing text in place. To ADD a fact under a section, prefer "
        "`edit_section` (it needs no anchor and survives concurrent edits). Multiple "
        "edits are matched against the original.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
}

_EDIT_SECTION_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_section",
        "description": (
            "Add content to an existing note by TARGETING its structure instead of "
            "pasting an exact anchor. Prefer this over edit_note for the common case of "
            "appending a new fact under a section — it does NOT require the section's "
            "current text, so it survives concurrent edits by other conversations.\n"
            "- `target`: a heading by its text (e.g. 'About', 'Mentions', 'Summary') or a "
            "block reference (e.g. '^fact-1'). Matched case-insensitively; must be unique.\n"
            "- `operation`: 'append' (default — add after the section's existing lines), "
            "'prepend' (add right under the heading), or 'replace' (replace the section "
            "body).\n"
            "- `text`: the line(s) to insert, e.g. '- Prefers async standups' or a dated "
            "'- 2026-06-27 — mentioned the Verizon migration'. Do NOT include the heading "
            "itself. Use edit_note for frontmatter and for surgical mid-line changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Heading text (no '#') or '^block-ref' to target.",
                },
                "text": {"type": "string", "description": "Line(s) to insert."},
                "operation": {
                    "type": "string",
                    "enum": ["append", "prepend", "replace"],
                },
            },
            "required": ["path", "target", "text"],
        },
    },
}

_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_note",
        "description": "Create a NEW note (conversation, person, or topic) at a "
        "vault-relative path. Fails if it already exists unless overwrite=true; prefer "
        "edit_note for existing notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
    },
}

_RENAME_TOOL = {
    "type": "function",
    "function": {
        "name": "rename_person",
        "description": "Rename a person note and rewrite ALL [[wikilinks]] to them "
        "across the vault (use after speaker re-identification, e.g. 'Speaker 0' -> "
        "'Alice'). If the new name already exists this merges into it.",
        "parameters": {
            "type": "object",
            "properties": {
                "old_name": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["old_name", "new_name"],
        },
    },
}

_CREATE_CATEGORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_category",
        "description": (
            "Mint a NEW organic category — its template, aggregation base, and hub note — "
            "for a substantive, recurring KIND of thing that isn't People/Topics/"
            "Conversations (e.g. Places, Projects, Books, Companies). Idempotent. Use "
            "sparingly: only when the thing will plausibly recur and matters; prefer an "
            "existing category. After this, file notes under '<name>/<Title>.md'.\n"
            "- `name`: the PLURAL category name, e.g. 'Places'.\n"
            "- `properties`: a few short, reusable frontmatter keys its notes carry, e.g. "
            "['location', 'type']. Reuse names already used by other categories where you can."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plural category name."},
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short reusable frontmatter keys for the category's notes.",
                },
            },
            "required": ["name"],
        },
    },
}

_VERIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "verify_vault",
        "description": (
            "Check every note you created or edited this run against the vault's "
            "rules — note schema, duplicated sections, illegal paths, and notes that "
            "differ from an existing one only by capitalisation. Returns the problems "
            "and how to fix each. Call this before your final message and fix "
            "everything it reports; it only ever reports problems YOU introduced."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

# Full write-agent toolset.
VAULT_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _GREP_TOOL,
    _GLOB_TOOL,
    _READ_TOOL,
    _EDIT_TOOL,
    _EDIT_SECTION_TOOL,
    _WRITE_TOOL,
    _RENAME_TOOL,
    _CREATE_CATEGORY_TOOL,
    _VERIFY_TOOL,
]

# Read-only subset for search.
VAULT_SEARCH_TOOL_SCHEMAS: List[Dict[str, Any]] = [_GREP_TOOL, _GLOB_TOOL, _READ_TOOL]
