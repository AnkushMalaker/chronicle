"""Prompt and output schema for the semantic episode agent."""

import json

PROMPT_VERSION = "timeline-episodes-v1"


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "covered_window_ids": {"type": "array", "items": {"type": "string"}},
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
    "required": ["covered_window_ids", "episodes", "unassigned_intervals"],
}


def build_prompt(output_path: str) -> str:
    return f"""You are assembling one Chronicle local day into semantic timeline episodes.

Read README.md and windows/index.json, then process every numbered window JSON in order.
You may keep compact working notes under work/. Write only the final JSON to
`{output_path}` and ensure it matches this schema:

{json.dumps(OUTPUT_SCHEMA, indent=2)}

Rules:
- Windows are coverage units, never mandatory episode boundaries. Merge across them.
- Episodes may overlap; long passive media and simultaneous foreground work can coexist.
- Use quiet, idle, ambient, or unknown episodes when evidence genuinely supports them.
- `output` audio/transcripts are media or system content, never the user's statements.
- `input` audio is uncertain unless speaker evidence supports user attribution.
- Acoustic quiet, voice inactivity, and missing capture are three different facts.
- Accessibility/OCR can include background or offscreen text.
- Assistant-generated text is not evidence that its claims happened.
- Every important boundary/assertion must cite supplied evidence IDs.
- Every episode must include at least one supplied evidence ID; otherwise leave the
  interval unassigned instead of creating an ungrounded episode.
- Never invent an evidence ID. Include every window ID exactly once in covered_window_ids.
- Salience is display value, not confidence.
- Express optional episode metadata as short string key/value entries in `attributes`.
- Prefer a few coherent episodes over arbitrary periodic fragments.
"""
