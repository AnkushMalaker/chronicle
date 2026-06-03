"""Rolling-summary memory provider — the "minimal layer" from mem-arch-new-aru.md.

Stores a single MongoDB document per user with two text fields:

- ``user_profile``: durable facts about the user (preferences, identity, etc.)
- ``rolling_summary``: time-stamped events/plans/activities, append-only

Retrieval returns both fields verbatim — no embeddings, no graph, no search.
The whole thesis of this architecture is that for personal-AI workloads the
context window is the retriever; the system just maintains what to put in it.

Compression: when ``rolling_summary`` exceeds ~80% of a token budget, one
LLM call rewrites the oldest 30% of the summary into a denser form. Newest
70% stays untouched so recent events keep their full fidelity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from advanced_omi_backend.database import get_database
from advanced_omi_backend.llm_client import get_llm_client

from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig

memory_logger = logging.getLogger("memory_service")

# Mongo collection: one doc per user
COLLECTION_NAME = "rolling_summary_state"

# Token budget — chars/4 approximation. Blog default is 12K tokens; compress
# at 80% (= 9.6K tokens ≈ 38_400 chars) by rewriting the oldest 30%.
DEFAULT_TOKEN_BUDGET = 12_000
COMPRESSION_THRESHOLD = 0.80
COMPRESSION_FRACTION = 0.30
CHARS_PER_TOKEN = 4  # English-text approximation; good enough for soft trigger


_EXTRACTION_PROMPT = """You extract durable personal memories from a user/assistant chat session.

Return a single JSON object with exactly two arrays of FLAT STRINGS (no nested objects, no dicts inside the arrays):
- "profile_updates": durable facts about the user (preferences, identity, occupation, relationships, possessions, ongoing projects). Stable facts unlikely to change next week. Avoid duplicating facts already in the existing profile.
- "summary_entries": time-grounded events, plans, decisions, or activities. Each entry MUST start with the date prefix in the format [YYYY-MM-DD HH:MM] copied from the transcript line.

CRITICAL — preserve every concrete detail VERBATIM. For each event, capture all WH-details that appear in the transcript:
- WHO: people, friends, colleagues, family by name.
- WHAT: titles (books/plays/movies/songs/games/podcasts), products, brands, dishes, model numbers (e.g., "Canon EOS 80D").
- WHERE: stores, retailers (e.g., "Target", "Trader Joe's"), restaurants, cities, parks, venues, addresses, websites.
- WHEN: full date+time from the transcript prefix, plus any relative dates the user mentions ("last Sunday", "two weeks ago").
- HOW MUCH / HOW MANY: prices, distances, durations, quantities — keep them exact.
- Do NOT abstract or summarize specifics away. If a detail was in the transcript, it MUST be in your output.

Format rules:
- Each array element is a single complete sentence (string), not a JSON object or dict.
- Each summary_entries sentence should be self-contained — a future reader who only sees that one line should know who/what/where/when.

Examples of BAD vs GOOD:
- BAD:  "Attended a play at the local community theater."
- GOOD: "Attended The Glass Menagerie at the local community theater on 2023-05-26, impressed by the lead actress's performance."
- BAD:  "Redeemed a $5 coupon last Sunday."
- GOOD: "Redeemed a $5 coupon on coffee creamer at Target last Sunday."
- BAD:  {{"occupation": "User Acquisition Manager"}}
- GOOD: "Works as a User Acquisition Manager at Hopper."
- BAD:  {{"play": "The Glass Menagerie"}}
- GOOD: "Attended The Glass Menagerie at the local community theater on 2023-05-26."

Output JSON only — no markdown fences, no commentary.

If nothing in the transcript is worth remembering, return {{"profile_updates": [], "summary_entries": []}}.

# Existing user profile (do not duplicate facts already here)
{profile_block}

# Transcript
{transcript}

# JSON output
"""


_COMPRESSION_PROMPT = """Compress the following older portion of a personal memory summary into a denser form. Preserve ALL distinct facts, names, dates, locations, numbers, and plans — only remove redundancy and verbose phrasing. Keep the same chronological order. Do NOT add facts that are not in the input.

# Older summary (to compress)
{older}

# Compressed summary (output only this, no commentary)
"""


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class RollingSummaryMemoryService(MemoryServiceBase):
    """Memory service backed by a single Mongo doc per user (no graph, no vectors)."""

    @property
    def provider_identifier(self) -> str:
        return "rolling_summary"

    def __init__(self, config: MemoryConfig):
        super().__init__()
        self.config = config
        self._llm = None  # llm_client.LLMClient — sync API, wrapped via to_thread
        self._db = None
        self._col = None
        self._token_budget = DEFAULT_TOKEN_BUDGET

    async def initialize(self) -> None:
        if self._initialized:
            return

        self._llm = get_llm_client()
        self._db = get_database()
        self._col = self._db[COLLECTION_NAME]
        await self._col.create_index("user_id", unique=True)

        self._initialized = True
        memory_logger.info(
            "Rolling-summary memory service initialized (token_budget=%d, model=%s)",
            self._token_budget,
            self._llm.get_default_model(),
        )

    # ---------- Public API ---------------------------------------------------

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
    ) -> Tuple[bool, List[str]]:
        await self._ensure_initialized()

        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info("rolling_summary: skip empty transcript %s", source_id)
            return True, []

        t_start = time.perf_counter()

        state = await self._get_state(user_id)
        existing_profile = state.get("user_profile", "")

        t_extract = time.perf_counter()
        try:
            extracted = await self._extract(transcript, existing_profile)
        except Exception as exc:
            memory_logger.error(
                "rolling_summary extract failed for %s: %s", source_id, exc
            )
            return False, []
        t_extract = time.perf_counter() - t_extract

        profile_updates: List[str] = self._coerce_strings(
            extracted.get("profile_updates") or []
        )
        summary_entries: List[str] = self._coerce_strings(
            extracted.get("summary_entries") or []
        )

        if not profile_updates and not summary_entries:
            memory_logger.info(
                "rolling_summary: extractor returned empty for %s (transcript_len=%d)",
                source_id,
                len(transcript),
            )
            return True, []

        new_profile = self._merge_profile(existing_profile, profile_updates)
        new_summary = self._append_summary(
            state.get("rolling_summary", ""), summary_entries
        )

        compressed = False
        t_compress = 0.0
        if self._estimate_tokens(new_summary) > int(
            self._token_budget * COMPRESSION_THRESHOLD
        ):
            t_compress = time.perf_counter()
            try:
                new_summary = await self._compress(new_summary)
                compressed = True
            except Exception as exc:
                memory_logger.warning(
                    "rolling_summary compression failed (keeping uncompressed): %s", exc
                )
            t_compress = time.perf_counter() - t_compress

        # Generate stable ids per appended fact so the runner sees a non-zero
        # count and downstream tooling has something to reference.
        seq_start = int(state.get("fact_count", 0))
        fact_ids = [
            f"rs_{user_id}_{seq_start + i}"
            for i in range(len(profile_updates) + len(summary_entries))
        ]

        await self._col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "user_profile": new_profile,
                    "rolling_summary": new_summary,
                    "summary_chars": len(new_summary),
                    "summary_tokens_est": self._estimate_tokens(new_summary),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_email": user_email,
                    "fact_count": seq_start + len(fact_ids),
                },
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            upsert=True,
        )

        t_total = time.perf_counter() - t_start
        memory_logger.info(
            "rolling_summary add_memory %s: profile+=%d summary+=%d "
            "compressed=%s tokens=%d/%d | extract=%.2fs compress=%.2fs total=%.2fs",
            source_id,
            len(profile_updates),
            len(summary_entries),
            compressed,
            self._estimate_tokens(new_summary),
            self._token_budget,
            t_extract,
            t_compress,
            t_total,
        )
        return True, fact_ids

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        """Return profile + summary verbatim — query is intentionally ignored.

        This is the whole point of the architecture: retrieval is "always pass
        the same two text blobs", trusting the answering LLM's context window
        to do the work a vector store would in other architectures.
        """
        await self._ensure_initialized()

        state = await self._col.find_one({"user_id": user_id})
        if not state:
            return []

        entries: List[MemoryEntry] = []
        profile = (state.get("user_profile") or "").strip()
        summary = (state.get("rolling_summary") or "").strip()

        if profile:
            entries.append(
                MemoryEntry(
                    id=f"profile:{user_id}",
                    content="## User Profile\n" + profile,
                    metadata={
                        "user_id": user_id,
                        "provider": self.provider_identifier,
                        "kind": "user_profile",
                    },
                    created_at=state.get("created_at"),
                    updated_at=state.get("updated_at"),
                )
            )
        if summary:
            entries.append(
                MemoryEntry(
                    id=f"summary:{user_id}",
                    content="## Rolling Summary\n" + summary,
                    metadata={
                        "user_id": user_id,
                        "provider": self.provider_identifier,
                        "kind": "rolling_summary",
                        "tokens_est": state.get("summary_tokens_est"),
                    },
                    created_at=state.get("created_at"),
                    updated_at=state.get("updated_at"),
                )
            )
        return entries

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        # Same content as search_memories; the architecture has no notion of
        # "more vs. fewer" memories — there's just the one blob.
        return await self.search_memories(query="", user_id=user_id, limit=limit)

    async def count_memories(self, user_id: str) -> Optional[int]:
        await self._ensure_initialized()
        state = await self._col.find_one({"user_id": user_id})
        if not state:
            return 0
        return int(state.get("fact_count", 0))

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        # Individual fact-level retrieval is not supported in this architecture.
        if not user_id:
            return None
        entries = await self.search_memories(query="", user_id=user_id)
        for e in entries:
            if e.id == memory_id:
                return e
        return None

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        # Individual fact deletion is not supported — facts are folded into
        # the summary text. Callers wanting a hard reset should use
        # delete_all_user_memories.
        memory_logger.debug(
            "rolling_summary: delete_memory not supported (id=%s)", memory_id
        )
        return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        await self._ensure_initialized()
        result = await self._col.delete_one({"user_id": user_id})
        return int(result.deleted_count)

    async def test_connection(self) -> bool:
        try:
            if self._llm is None:
                self._llm = get_llm_client()
            health = await self._llm.health_check()
            return bool(health.get("healthy", False))
        except Exception as exc:
            memory_logger.error("rolling_summary connection test failed: %s", exc)
            return False

    def shutdown(self) -> None:
        self._initialized = False

    # ---------- Internals ----------------------------------------------------

    async def _get_state(self, user_id: str) -> dict:
        doc = await self._col.find_one({"user_id": user_id})
        return doc or {}

    async def _llm_generate(self, prompt: str) -> str:
        """LLM call wrapped via to_thread because llm_client.generate is sync."""
        if self._llm is None:
            self._llm = get_llm_client()

        def _call() -> str:
            return self._llm.generate(prompt=prompt)

        return await asyncio.to_thread(_call)

    async def _extract(self, transcript: str, existing_profile: str) -> dict:
        prompt = _EXTRACTION_PROMPT.format(
            transcript=transcript,
            profile_block=existing_profile.strip() or "(empty)",
        )
        raw = await self._llm_generate(prompt)
        return self._parse_json(raw)

    async def _compress(self, summary: str) -> str:
        """Rewrite the oldest 30% of the summary, leaving the newest 70% intact."""
        # Split on newlines; keep the cut on a line boundary so we don't slice
        # mid-sentence. Older = first 30% of lines (chronological order is preserved
        # because we always append).
        lines = [ln for ln in summary.splitlines() if ln.strip()]
        if len(lines) < 4:
            # Too short to bother compressing. The caller's threshold check will
            # have already passed, so we accept slightly higher token use here
            # rather than dropping content.
            return summary

        cut = max(1, int(len(lines) * COMPRESSION_FRACTION))
        older = "\n".join(lines[:cut])
        newer = "\n".join(lines[cut:])

        prompt = _COMPRESSION_PROMPT.format(older=older)
        compressed_older = (await self._llm_generate(prompt)).strip()
        if not compressed_older:
            memory_logger.warning(
                "rolling_summary: empty compression result, keeping original"
            )
            return summary
        return compressed_older + "\n" + newer

    @staticmethod
    def _coerce_strings(items: list) -> List[str]:
        """Normalize LLM output to a list of strings.

        Some models obey the prompt and return ``["fact A", "fact B"]``. Others
        slip back into ``[{"fact": "..."}]`` or ``[{"date": "...", "text": "..."}]``.
        We accept both: dicts get json-dumped if they have multiple keys, or
        unwrapped if there's an obvious text-bearing key.
        """
        out: List[str] = []
        for item in items:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
                continue
            if isinstance(item, dict):
                # Common shapes the LLM falls back to.
                for k in ("text", "fact", "entry", "content", "summary", "value"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
                        break
                else:
                    # Fall back to a compact json dump so nothing is silently dropped.
                    out.append(json.dumps(item, ensure_ascii=False))
                continue
            # Anything else (number, None, list) — coerce to str if non-empty.
            s = str(item).strip()
            if s:
                out.append(s)
        return out

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return len(text) // CHARS_PER_TOKEN

    @staticmethod
    def _merge_profile(existing: str, updates: List[str]) -> str:
        """Append profile facts as bullet lines, dropping exact duplicates."""
        existing_lines = [
            ln.strip() for ln in (existing or "").splitlines() if ln.strip()
        ]
        existing_set = {ln.lstrip("- ").strip().lower() for ln in existing_lines}
        merged = list(existing_lines)
        for u in updates:
            u = u.strip()
            if not u:
                continue
            normalized = u.lstrip("- ").strip().lower()
            if normalized in existing_set:
                continue
            merged.append(f"- {u}" if not u.startswith("-") else u)
            existing_set.add(normalized)
        return "\n".join(merged)

    @staticmethod
    def _append_summary(existing: str, entries: List[str]) -> str:
        existing = (existing or "").rstrip()
        added = [e.strip() for e in entries if e.strip()]
        if not added:
            return existing
        if existing:
            return existing + "\n" + "\n".join(added)
        return "\n".join(added)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Best-effort JSON parse; falls back to extracting the first {...} block."""
        if not raw:
            return {"profile_updates": [], "summary_entries": []}
        s = raw.strip()
        # Strip markdown fences if the model added them
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:].lstrip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            m = _JSON_BLOCK.search(s)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        memory_logger.warning(
            "rolling_summary: extractor returned non-JSON (first 200 chars: %r)",
            s[:200],
        )
        return {"profile_updates": [], "summary_entries": []}
