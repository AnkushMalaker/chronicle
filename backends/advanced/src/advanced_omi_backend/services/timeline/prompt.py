"""Prompt and output schema for the semantic episode agent."""

import json

PROMPT_VERSION = "timeline-episodes-v6"


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "episodes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                    "salience": {
                        "type": "string",
                        "enum": ["background", "routine", "notable", "highlight"],
                    },
                    "activity_mode": {
                        "type": "string",
                        "enum": ["foreground", "background", "ambient", "idle"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "claim": {"type": "string"},
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "user_action",
                                        "user_statement",
                                        "third_party",
                                        "application_state",
                                        "media_content",
                                        "assistant_generated",
                                        "ambient",
                                        "uncertain",
                                    ],
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "evidence_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                            },
                            "required": ["claim", "role", "confidence", "evidence_ids"],
                        },
                    },
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "related_conversation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "parent_episode_index": {"type": ["integer", "null"]},
                    "representative_evidence_id": {"type": ["string", "null"]},
                },
                "required": [
                    "kind",
                    "title",
                    "summary",
                    "started_at",
                    "ended_at",
                    "salience",
                    "activity_mode",
                    "confidence",
                    "entities",
                    "attributes",
                    "assertions",
                    "evidence_ids",
                    "related_conversation_ids",
                    "parent_episode_index",
                    "representative_evidence_id",
                ],
            },
        },
        "unassigned_intervals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                    "reason": {"type": "string"},
                },
                "required": ["started_at", "ended_at", "reason"],
            },
        },
    },
    "required": ["episodes", "unassigned_intervals"],
}


def build_prompt(output_path: str) -> str:
    return f"""You are assembling one Chronicle local day into semantic timeline episodes.

Read README.md and windows/index.json, then process every numbered window JSON in order.
You may keep compact working notes under work/. Write only the final JSON to
`{output_path}` and ensure it matches this schema:

{json.dumps(OUTPUT_SCHEMA, indent=2)}

Rules:
- Windows are coverage units, never mandatory episode boundaries. Merge across them.
- A screen observation is a coarse application session, not a claim that exactly one
  semantic event occurred. Keep it whole when finer boundaries are unsupported; never
  manufacture periodic sub-events from its sparse samples.
- Do not merge distinct foreground applications, games, or participant contexts merely
  because they are adjacent. Use separate episodes, or a parent session with supported
  child events, when the evidence distinguishes them.
- Episodes may overlap; long passive media and simultaneous foreground work can coexist.
- Use quiet, idle, ambient, or unknown episodes when evidence genuinely supports them.
- `output` audio/transcripts are media or system content, never the user's statements.
- `input` audio is uncertain unless speaker evidence supports user attribution.
- Acoustic quiet, voice inactivity, and missing capture are three different facts.
- Accessibility/OCR can include background or offscreen text.
- Assistant-generated text is not evidence that its claims happened.
- Every episode start and end must be supported by cited evidence at or near that
  boundary. Use the exact timestamps from the first and last cited evidence instead of
  rounded times. Assertions must cite their supporting evidence IDs.
- Episode citations must temporally overlap the episode interval.
- Every episode must include at least one supplied evidence ID; otherwise leave the
  interval unassigned instead of creating an ungrounded episode.
- Account for every evidence-bearing interval with one or more episodes or an explicit
  `unassigned_interval`. Unassigned intervals must be positive and inside this day.
- Never return both `episodes` and `unassigned_intervals` empty when evidence exists.
- `representative_evidence_id` is optional and, when set, must name image evidence
  already cited by that episode. Use null when no suitable image is available.
- Never invent an evidence ID. Chronicle tracks authoritative window coverage itself;
  do not echo window IDs into the result.
- Salience is display value, not confidence.
- Express optional episode metadata as short string key/value entries in `attributes`.
- Prefer a few coherent episodes over arbitrary periodic fragments.
"""
