"""LoCoMo dataset loader.

LoCoMo (https://github.com/snap-research/locomo) is a long-term conversational
memory benchmark: 10 multi-session dialogues between two *named* speakers, each
with ~100-260 QA pairs across five categories. Unlike LongMemEval (user vs.
assistant), LoCoMo's named speakers map directly onto Chronicle's conversation
+ person model, which is exactly what the vault-first design wants to exercise.

Schema (top-level JSON array of 10 samples)::

    {
      "sample_id": "conv-26",
      "conversation": {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [{"speaker": "Caroline", "dia_id": "D1:1", "text": "..."}, ...],
        "session_2_date_time": "...",
        "session_2": [...],
        ...
      },
      "qa": [
        {"question": "...", "answer": "...", "evidence": ["D1:3"], "category": 2},
        {"question": "...", "evidence": ["D2:3"], "category": 5,
         "adversarial_answer": "..."},   # category 5 has no "answer"
        ...
      ]
    }

Categories (LoCoMo paper numbering): 1 multi-hop, 2 temporal, 3 open-domain,
4 single-hop, 5 adversarial (unanswerable). Category 5 is excluded by default —
mem0's published LoCoMo eval skips it, and its gold field differs.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
_DEFAULT_CACHE = Path.home() / ".cache" / "locomo" / "locomo10.json"

_CATEGORY_LABELS = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}

# Map LoCoMo categories onto the LongMemEval judge templates (judge.py). Temporal
# gets the off-by-one-tolerant rubric; everything else uses the default
# "response contains the correct answer" rubric. Category 5 is judged via the
# abstention template (is_abstention=True), so its judge_type is unused.
_JUDGE_TYPE = {
    1: "multi-session",
    2: "temporal-reasoning",
    3: "multi-session",
    4: "multi-session",
}

_SESSION_RE = re.compile(r"^session_(\d+)$")


@dataclass(frozen=True)
class LocomoTurn:
    speaker: str
    text: str


@dataclass(frozen=True)
class LocomoSession:
    """One dated session of a LoCoMo conversation (a single multi-speaker dialogue)."""

    session_id: str
    date: datetime  # tz-aware UTC; parsed from session_N_date_time
    date_str: str  # original human-readable LoCoMo string, kept for the transcript header
    turns: list[LocomoTurn]


@dataclass(frozen=True)
class LocomoQuestion:
    question_id: str  # f"{sample_id}-q{idx}"
    question: str
    answer: str  # for category 5, the adversarial_answer / explanation
    category: int
    category_label: str
    judge_type: str  # LongMemEval judge template key (see _JUDGE_TYPE)
    is_abstention: bool  # True for category 5
    evidence: list[str]


@dataclass(frozen=True)
class LocomoSample:
    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[LocomoSession]  # chronological
    questions: list[LocomoQuestion]


def _parse_date(value: object) -> Optional[datetime]:
    """Parse LoCoMo date strings like ``1:56 pm on 8 May, 2023``."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    candidates = [
        "%I:%M %p on %d %B, %Y",
        "%I:%M %p on %d %B %Y",
        "%H:%M on %d %B, %Y",
        "%d %B, %Y",
        "%d %B %Y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Unrecognized LoCoMo date %r — defaulting to None", value)
    return None


def _parse_sample(raw: dict, *, include_adversarial: bool) -> LocomoSample:
    conv = raw["conversation"]
    sample_id = str(raw["sample_id"])
    speaker_a = conv.get("speaker_a", "Speaker A")
    speaker_b = conv.get("speaker_b", "Speaker B")

    sessions: list[LocomoSession] = []
    for key, value in conv.items():
        m = _SESSION_RE.match(key)
        if not m or not isinstance(value, list):
            continue
        idx = int(m.group(1))
        date_str = str(conv.get(f"{key}_date_time", "") or "")
        date = _parse_date(date_str) or datetime(2023, 1, 1, tzinfo=timezone.utc)
        turns = [
            LocomoTurn(speaker=str(t.get("speaker", "") or "Unknown"), text=str(t.get("text", "") or ""))
            for t in value
            if t.get("text")
        ]
        if turns:
            sessions.append(
                LocomoSession(
                    session_id=f"{sample_id}_session_{idx}",
                    date=date,
                    date_str=date_str,
                    turns=turns,
                )
            )
    sessions.sort(key=lambda s: s.date)

    questions: list[LocomoQuestion] = []
    for i, qa in enumerate(raw.get("qa", [])):
        category = int(qa.get("category", 0))
        if category == 5:
            if not include_adversarial:
                continue
            answer = str(qa.get("adversarial_answer", qa.get("answer", "")) or "")
            judge_type = _JUDGE_TYPE.get(category, "multi-session")
            is_abstention = True
        else:
            answer = str(qa.get("answer", ""))
            judge_type = _JUDGE_TYPE.get(category, "multi-session")
            is_abstention = False
        questions.append(
            LocomoQuestion(
                question_id=f"{sample_id}-q{i}",
                question=str(qa.get("question", "")),
                answer=answer,
                category=category,
                category_label=_CATEGORY_LABELS.get(category, f"cat{category}"),
                judge_type=judge_type,
                is_abstention=is_abstention,
                evidence=list(qa.get("evidence") or []),
            )
        )

    return LocomoSample(
        sample_id=sample_id,
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        sessions=sessions,
        questions=questions,
    )


def _ensure_dataset(data_path: Optional[str | Path]) -> Path:
    """Return a local path to locomo10.json, downloading to a cache if needed."""
    if data_path is not None:
        p = Path(data_path)
        if not p.exists():
            raise FileNotFoundError(f"LoCoMo data not found at {p}")
        return p
    if _DEFAULT_CACHE.exists():
        return _DEFAULT_CACHE
    _DEFAULT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading LoCoMo dataset from %s -> %s", _LOCOMO_URL, _DEFAULT_CACHE)
    urllib.request.urlretrieve(_LOCOMO_URL, _DEFAULT_CACHE)  # noqa: S310 — fixed, trusted URL
    return _DEFAULT_CACHE


def load_locomo(
    data_path: Optional[str | Path] = None,
    *,
    include_adversarial: bool = False,
    limit: Optional[int] = None,
) -> Iterator[LocomoSample]:
    """Yield LoCoMo samples.

    Args:
        data_path: Local locomo10.json. If None, downloads the official file to
            ``~/.cache/locomo/`` (cached).
        include_adversarial: Include category-5 (adversarial/unanswerable) QA.
            Off by default to match mem0's published methodology.
        limit: Cap on number of conversations (samples), for smoke runs.
    """
    path = _ensure_dataset(data_path)
    with path.open(encoding="utf-8") as f:
        rows = json.load(f)
    for i, raw in enumerate(rows):
        if limit is not None and i >= limit:
            return
        yield _parse_sample(raw, include_adversarial=include_adversarial)
