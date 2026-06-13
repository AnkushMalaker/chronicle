"""Privacy/comfort screening for annotation exports.

Before a conversation's audio + transcript is shipped to an outside annotator,
the export can run a configurable "shareability" screen: an LLM applies a
user-authored policy to each transcript segment and flags the ones too
personal to share. The flagged segments' time ranges are then carved out of
the exported audio/transcript (see ``vad_analysis.subtract_intervals``).

This is deliberately NOT a PII redactor — names and identifiers are kept (the
annotator needs them for speaker labels). The policy targets personally
sensitive content (family, health, finances, confidential deals, …), and the
bar is the user's own comfort, not a regulatory definition.

This module is pure: it builds the prompt and parses the model's JSON. The LLM
call and DB access live in the RQ job (``workers/data_audit_jobs.py``).
"""

import json
import re
from typing import Any, Dict, List, Optional

from advanced_omi_backend.models.conversation import Conversation

# Conservative fallback used when config is missing (mirrors defaults.yml).
DEFAULT_SENSITIVITY_POLICY = (
    "Flag segments I would not be comfortable sharing with an outside "
    "annotator because they are personally sensitive — in particular my "
    "family/household or living situation, health (physical or mental), "
    "personal finances or specific expenses, romantic relationships, "
    "religious/political beliefs stated as personal conviction, or "
    "confidential business deals (named counterparties, terms, unreleased "
    "plans). Do NOT flag general technical or product discussion, abstract "
    "business strategy, publicly-known facts, or people's names used only for "
    "context (annotators need names for speaker labels)."
)


def screenable_segments(
    segments: List["Conversation.SpeakerSegment"],
) -> List[tuple]:
    """``(index, segment)`` pairs for speech segments worth screening.

    ``index`` is the position in the full active-segment list so the caller can
    map a flagged index back to the original segment's time range. Note/event
    markers and empty segments are skipped (nothing to screen).
    """
    pairs = []
    for index, seg in enumerate(segments):
        if seg.segment_type != Conversation.SegmentType.SPEECH:
            continue
        if not (seg.text or "").strip():
            continue
        pairs.append((index, seg))
    return pairs


def build_screening_prompt(policy: str, pairs: List[tuple]) -> str:
    """Prompt asking the LLM to flag policy-matching segments by index."""
    lines = []
    for index, seg in pairs:
        speaker = seg.identified_as or seg.speaker or "unknown"
        text = " ".join((seg.text or "").split())
        lines.append(f"[{index}] ({speaker}) {text}")
    numbered = "\n".join(lines)

    return (
        "You are a privacy screener preparing a recorded conversation for "
        "sharing with an external human annotator. Decide which transcript "
        "segments are too sensitive to share, according to this policy:\n"
        "--- POLICY ---\n"
        f"{policy.strip()}\n"
        "--- END POLICY ---\n\n"
        "The transcript is code-switched English/Hindi (Hinglish) ASR output "
        "with errors — judge the meaning, not the language or spelling. Flag a "
        "segment only when it clearly matches the policy; when unsure, do not "
        "flag it. Names used only to address or refer to someone are NOT a "
        "reason to flag.\n\n"
        "Return ONLY a JSON object of this exact shape:\n"
        '{"flagged": [{"index": <segment number>, "category": "<short label>", '
        '"reason": "<one short sentence>"}]}\n'
        'If nothing matches the policy, return {"flagged": []}.\n\n'
        "Segments:\n"
        f"{numbered}\n"
    )


def parse_flagged(raw: str, valid_indices: set) -> List[Dict[str, Any]]:
    """Parse the model's JSON into a clean flagged list.

    Tolerates code fences / surrounding prose; drops entries whose index is not
    a real screenable segment. Returns ``[{index, category, reason}]``.
    """
    payload = _extract_json_object(raw)
    if payload is None:
        raise ValueError(f"screening response was not JSON: {raw[:200]!r}")

    flagged: List[Dict[str, Any]] = []
    seen: set = set()
    for item in payload.get("flagged") or []:
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index not in valid_indices or index in seen:
            continue
        seen.add(index)
        flagged.append(
            {
                "index": index,
                "category": str(item.get("category") or "sensitive").strip()[:60],
                "reason": str(item.get("reason") or "").strip()[:300],
            }
        )
    return flagged


def _extract_json_object(raw: str) -> Optional[dict]:
    """Best-effort extraction of the first JSON object from an LLM response."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
