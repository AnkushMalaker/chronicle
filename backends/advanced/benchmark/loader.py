"""LongMemEval dataset loader.

Both upstream HF repos host single large JSON arrays, not parquet/arrow with
config splits, so ``datasets.load_dataset`` doesn't auto-detect them. We
download the raw file via ``huggingface_hub.hf_hub_download`` (cached on
disk under ``HF_HOME``) and stream it row-by-row with ``ijson`` so a
``--limit 1`` smoke doesn't pay for loading 2.7 GB.

Variants: ``s`` (default, ~115k tok / ~500 instances), ``m`` (~10× cost),
``oracle`` (evidence-only sanity check).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .ingest import Turn

logger = logging.getLogger(__name__)

# (repo_id, filename) pairs to try in order. The cleaned repo has proper .json
# extensions; the original repo files are extension-less but contain valid JSON.
_DATASET_FILES = {
    "s": [
        ("xiaowu0162/longmemeval-cleaned", "longmemeval_s_cleaned.json"),
        ("xiaowu0162/longmemeval", "longmemeval_s"),
    ],
    "m": [
        ("xiaowu0162/longmemeval-cleaned", "longmemeval_m_cleaned.json"),
        ("xiaowu0162/longmemeval", "longmemeval_m"),
    ],
    "oracle": [
        ("xiaowu0162/longmemeval-cleaned", "longmemeval_oracle.json"),
        ("xiaowu0162/longmemeval", "longmemeval_oracle"),
    ],
}


@dataclass(frozen=True)
class Session:
    """One backdated user/assistant chat session inside a LongMemEval instance."""

    session_id: str
    date: datetime  # tz-aware UTC; parsed from haystack_dates
    turns: list[Turn]


@dataclass(frozen=True)
class LongMemEvalInstance:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: Optional[datetime]
    sessions: list[Session]  # ordered by haystack_session_ids
    answer_session_ids: list[str]
    is_abstention: bool


def _parse_date(value: object) -> Optional[datetime]:
    """Parse the LongMemEval date strings (commonly ``YYYY/MM/DD (Day) HH:MM``)."""
    if not isinstance(value, str) or not value:
        return None
    candidates = [
        "%Y/%m/%d (%a) %H:%M",
        "%Y/%m/%d (%a)",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Unrecognized date format %r — defaulting to None", value)
    return None


def _row_to_instance(row: dict) -> LongMemEvalInstance:
    raw_sessions = row.get("haystack_sessions") or []
    session_ids = row.get("haystack_session_ids") or []
    session_dates = row.get("haystack_dates") or []

    sessions: list[Session] = []
    for idx, turns in enumerate(raw_sessions):
        sid = session_ids[idx] if idx < len(session_ids) else f"session-{idx}"
        date = _parse_date(session_dates[idx] if idx < len(session_dates) else "")
        if date is None:
            date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        normalized: list[Turn] = []
        for t in turns:
            role = t.get("role")
            content = t.get("content", "")
            if role == "user" or role == "assistant":
                normalized.append({"role": role, "content": content})
        sessions.append(Session(session_id=sid, date=date, turns=normalized))

    # Some LongMemEval rows have numeric answers (e.g. multi-session "how many"
    # questions where the ground truth is an int). Coerce to str so downstream
    # code that calls .encode() (judge cache key, prompt building) doesn't
    # blow up on type. Question is always a string in practice but coerce
    # defensively for symmetry.
    return LongMemEvalInstance(
        question_id=row["question_id"],
        question_type=row.get("question_type", "unknown"),
        question=str(row["question"]),
        answer=str(row.get("answer", "")),
        question_date=_parse_date(row.get("question_date") or ""),
        sessions=sessions,
        answer_session_ids=list(row.get("answer_session_ids") or []),
        is_abstention="_abs" in row["question_id"],
    )


def _download(variant: str) -> Path:
    """Download (cached) the raw JSON for ``variant`` from HF; return the local path."""
    from huggingface_hub import hf_hub_download

    if variant not in _DATASET_FILES:
        raise ValueError(
            f"Unknown variant {variant!r}; pick one of {list(_DATASET_FILES)}"
        )
    last_err: Optional[Exception] = None
    for repo_id, filename in _DATASET_FILES[variant]:
        try:
            path = hf_hub_download(
                repo_id=repo_id, filename=filename, repo_type="dataset"
            )
            logger.info("Loaded LongMemEval %s from %s/%s", variant, repo_id, filename)
            return Path(path)
        except Exception as exc:  # noqa: BLE001
            logger.info("hf_hub_download(%r, %r) failed: %s", repo_id, filename, exc)
            last_err = exc
    raise RuntimeError(
        f"Could not download LongMemEval variant {variant!r}: {last_err}"
    )


def load_longmemeval(
    variant: str = "s",
    limit: Optional[int] = None,
) -> Iterator[LongMemEvalInstance]:
    """Yield LongMemEval instances from Hugging Face.

    Streaming row-by-row with ``ijson`` keeps memory bounded; a ``--limit N``
    short-circuit returns after the Nth row without parsing the rest.
    """
    path = _download(variant)

    try:
        import ijson  # type: ignore[import-not-found]

        def _iter_rows():
            with path.open("rb") as f:
                yield from ijson.items(f, "item")
    except ImportError:
        logger.info("ijson not installed; falling back to full json.load")

        def _iter_rows():
            with path.open("r", encoding="utf-8") as f:
                rows = json.load(f)
            yield from rows

    yielded = 0
    for row in _iter_rows():
        if limit is not None and yielded >= limit:
            return
        yield _row_to_instance(row)
        yielded += 1
