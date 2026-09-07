"""Versioned, per-user operating memory for Chronicle's Pi memory agent.

This is deliberately separate from the semantic Obsidian vault. ``AGENTS.md`` is
trusted runtime guidance generated from Chronicle's own traces, while skill/script
candidates are inert design artifacts until a future replay gate promotes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from backend.config import DATA_DIR

from .vault_tools import VaultToolError

MAX_AGENTS_CHARS = 12_000
MAX_SKILL_CHARS = 16_000
MAX_SCRIPT_CHARS = 24_000
MAX_RECALL_CHARS = 8_000
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _base_dir() -> Path:
    return Path(os.getenv("PI_OPERATING_MEMORY_DIR", DATA_DIR / "pi_operating_memory"))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _bounded_text(value: Any, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VaultToolError(f"{name} must be non-empty text.")
    text = value.strip() + "\n"
    if len(text) > limit:
        raise VaultToolError(f"{name} exceeds the {limit:,}-character limit.")
    return text


class OperatingMemoryStore:
    """Filesystem store for one user's active guidance and candidate skills."""

    def __init__(self, user_id: str, root: Path | None = None):
        safe_user = Path(str(user_id)).name
        if not safe_user or safe_user != str(user_id):
            raise ValueError("Invalid operating-memory user id")
        self.user_id = safe_user
        self.root = Path(root) if root is not None else _base_dir() / safe_user
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def agents_path(self) -> Path:
        return self.root / "AGENTS.md"

    @property
    def state_path(self) -> Path:
        return self.root / ".optimizer-state.json"

    def read_agents(self) -> str:
        if not self.agents_path.is_file():
            return ""
        return self.agents_path.read_text(encoding="utf-8")[:MAX_AGENTS_CHARS]

    def list_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return content-free candidate metadata, newest first."""

        limit = max(1, min(int(limit), 500))
        candidates: list[dict[str, Any]] = []
        manifest_paths = sorted(
            (self.root / "candidates").glob("*/manifest.json"), reverse=True
        )[:limit]
        for manifest_path in manifest_paths:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError(f"Invalid candidate manifest: {manifest_path}")
            if (manifest_path.parent / "AGENTS.md").is_file():
                content_path = manifest_path.parent / "AGENTS.md"
                component = "agents"
            elif (manifest_path.parent / "SKILL.md").is_file():
                content_path = manifest_path.parent / "SKILL.md"
                component = "skill"
            else:
                raise ValueError(
                    f"Candidate content is missing: {manifest_path.parent}"
                )
            content = content_path.read_text(encoding="utf-8")
            candidates.append(
                {
                    **manifest,
                    "component": manifest.get("component") or component,
                    "content_chars": len(content),
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
        return candidates

    def read_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Read one bounded candidate for explicit human inspection."""

        candidate = self._candidate(candidate_id)
        manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError(f"Invalid candidate manifest: {candidate_id}")
        if (candidate / "AGENTS.md").is_file():
            content_name = "AGENTS.md"
            content = (candidate / content_name).read_text(encoding="utf-8")[
                :MAX_AGENTS_CHARS
            ]
        elif (candidate / "SKILL.md").is_file():
            content_name = "SKILL.md"
            content = (candidate / content_name).read_text(encoding="utf-8")[
                :MAX_SKILL_CHARS
            ]
        else:
            raise ValueError(f"Candidate content is missing: {candidate_id}")
        result = {
            "manifest": manifest,
            "content_name": content_name,
            "content": content,
        }
        script_name = manifest.get("script_name")
        if isinstance(script_name, str) and script_name:
            script_path = candidate / script_name
            if not script_path.is_file():
                raise ValueError(
                    f"Candidate script is missing: {candidate_id}/{script_name}"
                )
            result["script_name"] = script_name
            result["script"] = script_path.read_text(encoding="utf-8")[
                :MAX_SCRIPT_CHARS
            ]
        return result

    def list_revisions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return content-free active-guidance revision metadata, newest first."""

        limit = max(1, min(int(limit), 500))
        revisions: list[dict[str, Any]] = []
        paths = sorted((self.root / "history").glob("*.json"), reverse=True)[:limit]
        for path in paths:
            revision = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(revision, dict):
                raise ValueError(f"Invalid operating-memory revision: {path}")
            before = str(revision.get("before") or "")
            after = str(revision.get("after") or "")
            revisions.append(
                {
                    "revision_id": path.name,
                    "recorded_at": revision.get("recorded_at"),
                    "rationale": revision.get("rationale"),
                    "evidence_ids": revision.get("evidence_ids") or [],
                    "before_chars": len(before),
                    "after_chars": len(after),
                    "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                    "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
                }
            )
        return revisions

    def rollback_agents_revision(self, revision_id: str) -> str:
        """Restore the state before one revision and record the rollback itself."""

        revision_id = str(revision_id).strip()
        if Path(revision_id).name != revision_id or not revision_id.endswith(".json"):
            raise VaultToolError("Invalid operating-memory revision id.")
        source_path = self.root / "history" / revision_id
        if not source_path.is_file():
            raise VaultToolError(f"Unknown operating-memory revision: {revision_id}")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(source, dict) or "before" not in source:
            raise ValueError(f"Invalid operating-memory revision: {source_path}")

        current = self.read_agents()
        restored = str(source["before"] or "")[:MAX_AGENTS_CHARS]
        recorded_at = datetime.now(timezone.utc).isoformat()
        rollback_id = recorded_at.replace(":", "-") + ".json"
        revision = {
            "recorded_at": recorded_at,
            "rationale": f"Rollback to state before {revision_id}",
            "evidence_ids": [f"rollback:{revision_id}"],
            "before": current,
            "after": restored,
        }
        _atomic_text(
            self.root / "history" / rollback_id,
            json.dumps(revision, ensure_ascii=False, indent=2) + "\n",
        )
        if restored:
            _atomic_text(self.agents_path, restored)
        else:
            self.agents_path.unlink(missing_ok=True)
        self._retire_active_candidates(
            status="rolled_back",
            status_fields={
                "rolled_back_at": recorded_at,
                "rollback_source_revision_id": revision_id,
                "rolled_back_by_revision_id": rollback_id,
            },
        )
        return f"Rolled back AGENTS.md using {revision_id}; revision {rollback_id}."

    def has_active_skills(self) -> bool:
        return any((self.root / "skills").glob("*/SKILL.md"))

    def replace_agents(
        self,
        content: str,
        *,
        rationale: str,
        evidence_ids: Sequence[str],
    ) -> str:
        content = _bounded_text(content, name="AGENTS.md", limit=MAX_AGENTS_CHARS)
        rationale = _bounded_text(rationale, name="rationale", limit=2_000).strip()
        evidence = [str(item).strip() for item in evidence_ids if str(item).strip()]
        if not evidence:
            raise VaultToolError("At least one trace artifact hash is required.")

        prior = self.read_agents()
        recorded_at = datetime.now(timezone.utc).isoformat()
        revision = {
            "recorded_at": recorded_at,
            "rationale": rationale,
            "evidence_ids": evidence[:50],
            "before": prior,
            "after": content,
        }
        revision_name = recorded_at.replace(":", "-") + ".json"
        _atomic_text(
            self.root / "history" / revision_name,
            json.dumps(revision, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_text(self.agents_path, content)
        return f"Updated AGENTS.md ({len(content):,} chars); revision {revision_name}."

    def propose_agents(
        self,
        content: str,
        *,
        rationale: str,
        evidence_ids: Sequence[str],
    ) -> str:
        """Record candidate guidance without changing the active Pi prompt."""

        content = _bounded_text(content, name="AGENTS.md", limit=MAX_AGENTS_CHARS)
        rationale = _bounded_text(rationale, name="rationale", limit=2_000).strip()
        evidence = [str(item).strip() for item in evidence_ids if str(item).strip()]
        if not evidence:
            raise VaultToolError("At least one trace artifact hash is required.")

        recorded_at = datetime.now(timezone.utc).isoformat()
        candidate_id = recorded_at.replace(":", "-") + "-agents"
        candidate = self.root / "candidates" / candidate_id
        active = self.read_agents()
        manifest = {
            "format": "chronicle-pi-agents-candidate-v1",
            "candidate_id": candidate_id,
            "component": "agents",
            "recorded_at": recorded_at,
            "rationale": rationale,
            "evidence_ids": evidence[:50],
            "active_agents_sha256": hashlib.sha256(active.encode()).hexdigest(),
            "candidate_agents_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "status": "shadow_candidate",
        }
        _atomic_text(candidate / "AGENTS.md", content)
        _atomic_text(
            candidate / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return (
            f"Recorded shadow AGENTS.md candidate {candidate_id}. It is not loaded "
            "by production Pi."
        )

    def review_agents_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        rationale: str,
        evidence_ids: Sequence[str],
    ) -> str:
        """Approve or reject one shadow candidate without activating it."""

        candidate = self._agents_candidate(candidate_id)
        decision = str(decision).strip().lower()
        if decision not in {"approve", "reject"}:
            raise VaultToolError("decision must be 'approve' or 'reject'.")
        rationale = _bounded_text(rationale, name="rationale", limit=4_000).strip()
        evidence = [str(item).strip() for item in evidence_ids if str(item).strip()]
        if not evidence:
            raise VaultToolError("At least one evaluation artifact is required.")
        if decision == "approve" and len(set(evidence)) < 2:
            raise VaultToolError(
                "Approval requires distinct development and holdout evaluation artifacts."
            )
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") not in {"shadow_candidate", "approved"}:
            raise VaultToolError(
                f"Candidate {candidate_id} cannot be reviewed from status "
                f"{manifest.get('status')!r}."
            )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        manifest.update(
            {
                "status": "approved" if decision == "approve" else "rejected",
                "reviewed_at": reviewed_at,
                "review_rationale": rationale,
                "evaluation_evidence_ids": evidence[:100],
            }
        )
        _atomic_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        past_tense = "Approved" if decision == "approve" else "Rejected"
        return f"{past_tense} shadow candidate {candidate_id}; active guidance is unchanged."

    def promote_agents_candidate(self, candidate_id: str) -> str:
        """Activate one explicitly approved candidate and retain rollback history."""

        candidate = self._agents_candidate(candidate_id)
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "approved":
            raise VaultToolError(
                f"Candidate {candidate_id} must be approved before promotion."
            )
        expected_active_hash = str(manifest.get("active_agents_sha256") or "")
        current_active_hash = hashlib.sha256(self.read_agents().encode()).hexdigest()
        if expected_active_hash != current_active_hash:
            raise VaultToolError(
                f"Candidate {candidate_id} is stale because active AGENTS.md changed "
                "after the proposal was created. Evaluate a new candidate instead."
            )
        content = (candidate / "AGENTS.md").read_text(encoding="utf-8")
        result = self.replace_agents(
            content,
            rationale=str(
                manifest.get("review_rationale") or manifest.get("rationale")
            ),
            evidence_ids=manifest.get("evaluation_evidence_ids")
            or manifest.get("evidence_ids")
            or [],
        )
        promoted_at = datetime.now(timezone.utc).isoformat()
        self._retire_active_candidates(
            status="superseded",
            status_fields={"superseded_at": promoted_at, "superseded_by": candidate_id},
        )
        manifest.update(
            {
                "status": "active",
                "promoted_at": promoted_at,
            }
        )
        _atomic_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return f"Promoted {candidate_id}. {result}"

    def _retire_active_candidates(
        self, *, status: str, status_fields: dict[str, Any]
    ) -> None:
        for manifest_path in sorted((self.root / "candidates").glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError(f"Invalid candidate manifest: {manifest_path}")
            if manifest.get("status") != "active":
                continue
            manifest.update({"status": status, **status_fields})
            _atomic_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

    def _agents_candidate(self, candidate_id: str) -> Path:
        candidate = self._candidate(candidate_id)
        if not (candidate / "AGENTS.md").is_file():
            raise VaultToolError(
                f"Candidate is not an AGENTS.md proposal: {candidate_id}"
            )
        return candidate

    def _candidate(self, candidate_id: str) -> Path:
        candidate_id = str(candidate_id).strip()
        if Path(candidate_id).name != candidate_id or not candidate_id:
            raise VaultToolError("Invalid operating-memory candidate id.")
        candidate = self.root / "candidates" / candidate_id
        if not candidate.is_dir() or not (candidate / "manifest.json").is_file():
            raise VaultToolError(f"Unknown operating-memory candidate: {candidate_id}")
        return candidate

    def write_skill_candidate(
        self,
        *,
        slug: str,
        skill_markdown: str,
        rationale: str,
        evidence_ids: Sequence[str],
        script_name: str = "",
        script: str = "",
    ) -> str:
        slug = str(slug).strip().lower()
        if not _SAFE_SLUG.fullmatch(slug):
            raise VaultToolError(
                "slug must be 2-64 lowercase letters, digits, underscores, or hyphens."
            )
        skill_markdown = _bounded_text(
            skill_markdown, name="skill_markdown", limit=MAX_SKILL_CHARS
        )
        rationale = _bounded_text(rationale, name="rationale", limit=2_000).strip()
        evidence = [str(item).strip() for item in evidence_ids if str(item).strip()]
        if not evidence:
            raise VaultToolError("At least one trace artifact hash is required.")

        script_name = str(script_name).strip()
        if script_name:
            if Path(script_name).name != script_name or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", script_name
            ):
                raise VaultToolError("script_name must be one plain filename.")
            script = _bounded_text(script, name="script", limit=MAX_SCRIPT_CHARS)
        elif str(script).strip():
            raise VaultToolError("script_name is required when script is provided.")

        recorded_at = datetime.now(timezone.utc).isoformat()
        candidate_id = recorded_at.replace(":", "-") + f"-{slug}"
        candidate = self.root / "candidates" / candidate_id
        manifest = {
            "format": "chronicle-pi-skill-candidate-v1",
            "candidate_id": candidate_id,
            "slug": slug,
            "recorded_at": recorded_at,
            "rationale": rationale,
            "evidence_ids": evidence[:50],
            "script_name": script_name or None,
            "status": "inert_candidate",
        }
        _atomic_text(candidate / "SKILL.md", skill_markdown)
        if script_name:
            _atomic_text(candidate / script_name, script)
        _atomic_text(
            candidate / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return (
            f"Recorded inert skill candidate {candidate_id}. It is not executable or "
            "loaded by production Pi."
        )

    def recall(self, query: str, limit: int = 5) -> str:
        """Return bounded, query-ranked operating guidance chosen by Pi."""
        terms = {term.casefold() for term in re.findall(r"[\w-]{3,}", str(query))}
        limit = max(1, min(int(limit), 10))
        documents: list[tuple[int, str, str]] = []
        agents = self.read_agents()
        if agents:
            score = sum(agents.casefold().count(term) for term in terms)
            documents.append((score, "AGENTS.md", agents))
        for path in sorted((self.root / "skills").glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL_CHARS]
            score = sum(text.casefold().count(term) for term in terms)
            documents.append((score, path.relative_to(self.root).as_posix(), text))
        documents.sort(key=lambda item: (-item[0], item[1]))
        selected = documents[:limit]
        if terms and any(score > 0 for score, _path, _text in documents):
            selected = [item for item in documents if item[0] > 0][:limit]
        if not selected:
            return "No operating-memory guidance is available."
        rendered = "\n\n".join(f"## {path}\n{text}" for _score, path, text in selected)
        return rendered[:MAX_RECALL_CHARS]

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"processed_artifact_hashes": []}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Invalid optimizer state: {self.state_path}")
        return value

    def mark_processed(self, artifact_hashes: Sequence[str]) -> None:
        state = self.load_state()
        prior = [str(item) for item in state.get("processed_artifact_hashes", [])]
        merged = list(dict.fromkeys([*prior, *map(str, artifact_hashes)]))[-2_000:]
        state.update(
            {
                "processed_artifact_hashes": merged,
                "last_optimized_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _atomic_text(self.state_path, json.dumps(state, indent=2) + "\n")

    @contextmanager
    def optimization_lease(self, *, stale_after_minutes: int = 60) -> Iterator[bool]:
        """Acquire a cross-process best-effort lease for one user's optimizer."""
        lease = self.root / ".optimizer-lease"
        try:
            fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
            modified = datetime.fromtimestamp(lease.stat().st_mtime, tz=timezone.utc)
            if modified >= cutoff:
                yield False
                return
            lease.unlink(missing_ok=True)
            fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, datetime.now(timezone.utc).isoformat().encode())
            os.close(fd)
            yield True
        finally:
            lease.unlink(missing_ok=True)


RECALL_OPERATING_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_operating_memory",
        "description": (
            "Recall bounded prior operating lessons or active skills. You choose the "
            "query and still choose which vault files to inspect."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    },
}

_OPTIMIZER_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_operating_memory",
        "description": "Read the current active AGENTS.md operating guidance.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_OPTIMIZER_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_skill_candidate",
        "description": (
            "Record a reusable skill and optional script as an inert candidate. "
            "Candidates are never executed or loaded automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "skill_markdown": {"type": "string"},
                "rationale": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "script_name": {"type": "string"},
                "script": {"type": "string"},
            },
            "required": ["slug", "skill_markdown", "rationale", "evidence_ids"],
        },
    },
}


def optimizer_tool_schemas(mode: str) -> list[dict[str, Any]]:
    """Expose an unambiguous write tool for the configured promotion boundary."""

    if mode not in {"shadow", "active"}:
        raise ValueError("operating-memory mode must be 'shadow' or 'active'")
    if mode == "shadow":
        agents_schema = {
            "type": "function",
            "function": {
                "name": "propose_agents_memory",
                "description": (
                    "Record a shadow AGENTS.md candidate for isolated replay. It does "
                    "not change guidance loaded by production Pi."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "rationale": {"type": "string"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["content", "rationale", "evidence_ids"],
                },
            },
        }
    else:
        agents_schema = {
            "type": "function",
            "function": {
                "name": "replace_agents_memory",
                "description": (
                    "Replace active AGENTS.md with a bounded improved version. Keep "
                    "useful rules, cite trace hashes, and avoid fixed file routing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "rationale": {"type": "string"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["content", "rationale", "evidence_ids"],
                },
            },
        }
    return [_OPTIMIZER_READ_SCHEMA, agents_schema, _OPTIMIZER_SKILL_SCHEMA]


class OperatingMemoryTools:
    """Tool handler used by the isolated Pi optimizer."""

    mutating_tools = frozenset(
        {"propose_agents_memory", "replace_agents_memory", "write_skill_candidate"}
    )

    def __init__(self, store: OperatingMemoryStore, *, mode: str = "shadow"):
        if mode not in {"shadow", "active"}:
            raise ValueError("operating-memory mode must be 'shadow' or 'active'")
        self.store = store
        self.mode = mode
        self.touched: list[str] = []
        self.removed: list[dict[str, Any]] = []
        self.verified = True
        self._changed_component = ""

    def _claim_component(self, component: str) -> None:
        if self._changed_component and self._changed_component != component:
            raise VaultToolError(
                "Only one operating-memory component may change per optimizer run."
            )
        self._changed_component = component

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_operating_memory":
            return self.store.read_agents() or "AGENTS.md is currently empty."
        if name == "propose_agents_memory":
            if self.mode != "shadow":
                raise VaultToolError("Shadow proposals are disabled in active mode.")
            self._claim_component("agents")
            result = self.store.propose_agents(
                arguments.get("content"),
                rationale=arguments.get("rationale"),
                evidence_ids=arguments.get("evidence_ids") or [],
            )
            self.touched.append("candidates/")
            return result
        if name == "replace_agents_memory":
            if self.mode != "active":
                raise VaultToolError(
                    "Active AGENTS.md replacement is disabled in shadow mode."
                )
            self._claim_component("agents")
            result = self.store.replace_agents(
                arguments.get("content"),
                rationale=arguments.get("rationale"),
                evidence_ids=arguments.get("evidence_ids") or [],
            )
            self.touched.append("AGENTS.md")
            return result
        if name == "write_skill_candidate":
            self._claim_component("skill_candidate")
            result = self.store.write_skill_candidate(
                slug=arguments.get("slug"),
                skill_markdown=arguments.get("skill_markdown"),
                rationale=arguments.get("rationale"),
                evidence_ids=arguments.get("evidence_ids") or [],
                script_name=arguments.get("script_name") or "",
                script=arguments.get("script") or "",
            )
            self.touched.append("candidates/")
            return result
        raise VaultToolError(f"Unknown operating-memory tool: {name}")


class VaultWithOperatingMemoryTools:
    """Add agent-owned recall to canonical vault tools without changing routing."""

    def __init__(self, vault_tools: Any, store: OperatingMemoryStore):
        self.vault_tools = vault_tools
        self.store = store

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "recall_operating_memory":
            return self.store.recall(
                arguments.get("query", ""), arguments.get("limit", 5)
            )
        return self.vault_tools.dispatch(name, arguments)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.vault_tools, name)
