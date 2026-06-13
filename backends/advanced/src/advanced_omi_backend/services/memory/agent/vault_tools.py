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
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List

from ..vault_lock import VaultLockTimeout, vault_note_lock
from ..vault_scaffold import write_category
from .edit_engine import Edit, EditError, apply_edits

logger = logging.getLogger("memory_service.agent.tools")

_GREP_MAX_LINES = 200  # default head limit, like Claude Code's grep


class VaultToolError(Exception):
    """Tool-level failure. The message is returned to the model so it can retry."""


def _safe_relpath(path: str) -> str:
    """Reject absolute paths, traversal, and slash-in-title nesting; normalise to a
    vault-relative ``<Folder>/<Title>.md`` (or top-level hub ``<Title>.md``) path.

    Whitespace around components is stripped — a title like ``"TailScale "`` would
    otherwise mint a trailing-space file/folder name that breaks Windows and Syncthing.
    """
    p = path.strip().lstrip("/")
    if ".." in Path(p).parts:
        raise VaultToolError(f"Invalid path '{path}': must stay inside the vault.")
    if not p.endswith(".md"):
        p += ".md"
    parts = list(Path(p).parts)
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
    return str(Path(*dirs, stem + ".md"))


class VaultTools:
    """Filesystem-scoped tool implementations for one user's vault."""

    def __init__(self, vault_root: Path):
        self.root = Path(vault_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._rg = shutil.which("rg")
        self._notesmd = os.getenv("NOTESMD_CLI_BIN") or shutil.which("notesmd-cli")
        self.touched: set = set()  # vault-relative paths created/edited this run

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise ONE mutation against concurrent writers of this user's vault.

        The vault root's directory name is the user id (``conversation_docs/<user_id>``).
        Every mutating tool runs its full resolve-check-write sequence inside this lock
        so exists/case-collision checks cannot race a concurrent agent's write.
        """
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

    def _resolve_ci(self, rel: str) -> str:
        """Map a vault-relative path onto an existing file matching case-insensitively.

        The backend filesystem is case-sensitive, but macOS/Windows clients are not.
        Writing ``People/Hermes.md`` when ``People/hermes.md`` already exists would
        create a case-variant sibling here that collides irrecoverably once Syncthing
        pushes both to a case-insensitive client. Resolving each path component
        against what is already on disk makes the agent reuse the existing note
        instead, so two notes never differ only by case.
        """
        current = self.root
        resolved: List[str] = []
        parts = Path(rel).parts
        for i, part in enumerate(parts):
            if (current / part).exists():
                current = current / part
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
                resolved.append(match)
            else:
                resolved.extend(parts[i:])  # no match: keep requested casing onward
                break
        return str(Path(*resolved)) if resolved else rel

    def _abs(self, path: str) -> Path:
        return self.root / self._resolve_ci(_safe_relpath(path))

    def _all_md(self) -> List[Path]:
        return list(self.root.rglob("*.md"))

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
                for p in self.root.rglob("*.md")
                if Path(str(p.relative_to(self.root))).match(pattern)
            )
        return out or "No files found."

    # --- read / write -------------------------------------------------------

    def read_note(self, path: str) -> str:
        fp = self._abs(path)
        if not fp.exists():
            raise VaultToolError(
                f"Note '{path}' does not exist. Use glob or grep to find the right "
                f"path, or write_note to create it."
            )
        return fp.read_text(encoding="utf-8")

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
            fp.write_text(new_content, encoding="utf-8")
            self.touched.add(self._resolve_ci(_safe_relpath(path)))
        return f"Edited {path} ({len(edits)} replacement(s))."

    def write_note(self, path: str, content: str, overwrite: bool = False) -> str:
        with self._locked():
            rel = self._resolve_ci(_safe_relpath(path))
            fp = self.root / rel
            if fp.exists() and not overwrite:
                raise VaultToolError(
                    f"Note '{rel}' already exists. Use edit_note to modify it, or pass "
                    f"overwrite=true only if you intend to replace it entirely."
                )
            existed = fp.exists()
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            self.touched.add(rel)
        return f"{'Overwrote' if existed else 'Wrote'} {rel} ({len(content)} chars)."

    def create_category(self, name: str, properties: List[str] | None = None) -> str:
        """Mint a new organic category: its template, base, and hub note (idempotent).

        ``name`` should be the plural category name (e.g. ``"Places"``); ``properties`` the
        short, reusable frontmatter keys its notes carry (e.g. ``["location", "type"]``).
        """
        with self._locked():
            created = write_category(self.root, name, properties or [])
        for rel in created:
            self.touched.add(rel)
        if created:
            return (
                f"Created category '{name}' ({', '.join(created)}). Now file notes under "
                f'{name}/<Title>.md with categories: ["[[{name}]]"], using '
                f"Templates/{name} Template.md as the shape."
            )
        return (
            f"Category '{name}' already exists. File notes under {name}/<Title>.md and read "
            f"Templates/{name} Template.md for its schema."
        )

    def rename_person(self, old_name: str, new_name: str) -> str:
        old_rel, new_rel = f"People/{old_name}.md", f"People/{new_name}.md"
        with self._locked():
            old_fp, new_fp = self._abs(old_rel), self._abs(new_rel)
            if not old_fp.exists():
                raise VaultToolError(
                    f"Person note 'People/{old_name}.md' does not exist."
                )
            if new_fp.exists():
                # Merge case — a plain move would clobber the target. Rewrite backlinks in
                # Python, leave the bodies for the agent to consolidate via edit_note.
                n = self._rewrite_backlinks_python(old_name, new_name)
                old_fp.unlink()
                return (
                    f"'{new_name}' already existed — merged: rewrote {n} backlink(s) and "
                    f"deleted People/{old_name}.md. Review People/{new_name}.md and use "
                    f"edit_note to consolidate any duplicated facts."
                )
            self.touched.add(new_rel)
            if self._notesmd:
                try:
                    self._move_cli(old_rel, new_rel)
                    return f"Renamed People/{old_name} -> People/{new_name} (backlinks rewritten)."
                except Exception as e:  # noqa: BLE001
                    logger.warning("notesmd-cli move failed (%s); using python", e)
            n = self._rewrite_backlinks_python(old_name, new_name)
            old_fp.rename(new_fp)
        return f"Renamed People/{old_name} -> People/{new_name} ({n} backlink(s) rewritten)."

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
                changed += 1
        return changed

    # --- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, args: Dict[str, Any]) -> str:
        """Run a tool by name; always returns a string for the tool message."""
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
            return self.read_note(args["path"])
        if name == "edit_note":
            return self.edit_note(args["path"], args["edits"])
        if name == "write_note":
            return self.write_note(
                args["path"], args["content"], args.get("overwrite", False)
            )
        if name == "rename_person":
            return self.rename_person(args["old_name"], args["new_name"])
        if name == "create_category":
            return self.create_category(args["name"], args.get("properties"))
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
        "description": "Read a note's full markdown by vault-relative path "
        "(e.g. 'People/Alice.md'). Read before you edit.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_note",
        "description": "Apply exact string replacements to an existing note. Each edit's "
        "old_text must match the current file EXACTLY (whitespace included) and be "
        "UNIQUE — include surrounding context. Edit frontmatter as text here. Multiple "
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

# Full write-agent toolset.
VAULT_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _GREP_TOOL,
    _GLOB_TOOL,
    _READ_TOOL,
    _EDIT_TOOL,
    _WRITE_TOOL,
    _RENAME_TOOL,
    _CREATE_CATEGORY_TOOL,
]

# Read-only subset for search.
VAULT_SEARCH_TOOL_SCHEMAS: List[Dict[str, Any]] = [_GREP_TOOL, _GLOB_TOOL, _READ_TOOL]
