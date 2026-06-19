"""Tiered command cascade: router -> /conversation -> fuzzy-LLM -> Hermes.

Pure logic, dependency-injected so the SAME code path runs in:
  - the Home Assistant plugin (on_wake_word_detected), and
  - the standalone route_test.py harness.

Injected callables (all async):
  conversation_fn(text)              -> ConvResult           (HA /api/conversation/process)
  llm_complete_fn(system, user)      -> str                  (any chat LLM)
  hermes_fn(text)                    -> str                  (Hermes agent reply)

The cascade owns the Tier-2 "fuzzy intent -> canonical HA commands" prompt so
both callers translate identically.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# Areas/labels known to HA Assist (used to ground the Tier-2 translator).
DEFAULT_AREAS = ["hall", "living room", "dining room", "study"]

# Vocabulary Home Assistant understands via /conversation: built-in intents plus
# our custom_sentences in ha-config (relative_light.yaml + intent_script.yaml --
# warmer/cooler/dimmer/brighter/cozy/focus). The Tier-2 LLM only runs when the raw
# command didn't match HA directly; it maps a novel/fuzzy request onto these
# commands and HA executes them. (No hardcoded colour/brightness values here --
# "warm white" via /conversation is broken on our bulbs; the relative commands and
# cozy/focus do the actual color_temp_kelvin work HA-side.)
_TRANSLATE_SYSTEM = """You convert a casual smart-home lighting request into one or more explicit Home Assistant voice commands. Home Assistant executes exactly what you output.

Available areas: {areas} (omit the area to mean all lights that are on).

Use ONLY these command patterns:
  - "turn on [the] [<area>] lights"  /  "turn off [the] [<area>] lights"
  - "set the [<area>] lights to <N> percent"
  - "make [the] [<area>] lights warmer"  /  "make [the] [<area>] lights cooler"
  - "dim [the] [<area>] lights"  /  "brighten [the] [<area>] lights"
  - "make it cozy"   (= warmer + dimmer)
  - "focus"          (= cooler + brighter)

Guidance:
  - relaxing / soothing / soft / evening / movie / bedtime / romantic -> "make it cozy"
  - working / reading / alert -> "focus"
  - "too bright" -> "dim the lights";  "too dark" -> "brighten the lights"
  - "too warm/orange" -> "make the lights cooler";  "too cold/blue" -> "make the lights warmer"
  - For a named room, include the area (e.g. "make the study lights warmer").
  - If the request is NOT about controlling lights, output {{"commands": []}}.

Output STRICT JSON only: {{"commands": ["...", "..."]}}"""


@dataclass
class RouteInfo:
    """What an intent-router classification returns (route + confidence)."""

    route: str  # "home" | "other"
    p_home: float
    latency_ms: float = 0.0


@dataclass
class ConvResult:
    response_type: str  # action_done | query_answer | error
    code: Optional[str]
    speech: str
    success_count: int  # entities actually touched

    @property
    def confident_hit(self) -> bool:
        """Did HA handle the command?

        The intent router already gated chat out before /conversation, so any
        matched HA intent counts -- built-in OR our custom_sentences. Custom
        intent_scripts (warmer/cozy/dimmer...) return action_done with an empty
        'success' list, so we must NOT require success_count > 0 here.
        """
        return self.response_type in ("action_done", "query_answer")


@dataclass
class TierTrace:
    tier: str
    detail: str
    latency_ms: float


@dataclass
class CascadeResult:
    command: str
    final_tier: str = ""
    message: str = ""
    success: bool = False
    traces: List[TierTrace] = field(default_factory=list)
    total_ms: float = 0.0


def parse_translate(text: str) -> List[str]:
    """Parse the Tier-2 LLM JSON into a list of canonical commands."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        text = text.strip()
    try:
        data = json.loads(text)
        cmds = data.get("commands", [])
        return [c for c in cmds if isinstance(c, str) and c.strip()]
    except Exception as e:
        logger.warning("Tier-2 translate parse failed: %s (raw=%r)", e, text[:200])
        return []


async def run_ha_cascade(
    command: str,
    *,
    conversation_fn: Callable[[str], Awaitable[ConvResult]],
    llm_complete_fn: Callable[[str, str], Awaitable[str]],
    areas: Optional[List[str]] = None,
) -> CascadeResult:
    """Try to handle a command with Home Assistant: /conversation (fast path)
    -> LLM maps a novel fuzzy request onto HA's vocabulary, re-run via /conversation.

    Returns a CascadeResult whose ``success`` is True iff HA actually handled the
    command. On False, the caller declines (returns None) so the plugin chain
    passes the command to the next handler (e.g. the Hermes agent). This function
    knows nothing about Hermes or the intent router - those live one layer up.
    """
    areas = areas or DEFAULT_AREAS
    t_start = time.time()
    res = CascadeResult(command=command)

    # ---- Tier 1: HA /conversation (fast path) ----
    t = time.time()
    conv = await conversation_fn(command)
    res.traces.append(
        TierTrace(
            "ha_conversation",
            f"{conv.response_type}/{conv.code} touched={conv.success_count} :: {conv.speech}",
            (time.time() - t) * 1000,
        )
    )
    if conv.confident_hit:
        res.final_tier, res.message, res.success = "ha_conversation", conv.speech, True
        res.total_ms = (time.time() - t_start) * 1000
        return res

    # ---- Tier 2: LLM maps a novel fuzzy request onto HA's vocabulary ----
    t = time.time()
    tier2_label = "ha_fuzzy_llm"
    sys_prompt = _TRANSLATE_SYSTEM.format(areas=", ".join(areas))
    raw = await llm_complete_fn(sys_prompt, command)
    cmds = parse_translate(raw)
    res.traces.append(
        TierTrace("ha_fuzzy_llm", f"translated -> {cmds}", (time.time() - t) * 1000)
    )

    if cmds:
        executed, speeches = 0, []
        for c in cmds:
            cr = await conversation_fn(c)
            if cr.response_type in ("action_done", "query_answer") and cr.code is None:
                executed += 1
                speeches.append(cr.speech)
        if executed:
            res.final_tier = tier2_label
            res.message = "; ".join(s for s in speeches if s) or "Done"
            res.success = True
            res.total_ms = (time.time() - t_start) * 1000
            return res

    # Not a home command HA could act on -> decline; the chain moves on.
    res.final_tier, res.success = "declined", False
    res.total_ms = (time.time() - t_start) * 1000
    return res
