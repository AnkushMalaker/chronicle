"""LLM-based entity and relationship extraction from conversations.

This module uses the configured LLM provider to extract entities,
relationships, and promises from conversation transcripts.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.openai_factory import create_openai_client

from .models import (
    EntityType,
    ExtractedEntity,
    ExtractedPromise,
    ExtractedRelationship,
    ExtractionResult,
    RelationshipType,
)

logger = logging.getLogger("knowledge_graph")


ENTITY_EXTRACTION_PROMPT = """You are an entity extraction system. Extract entities, relationships, and promises from conversation transcripts.

ENTITY TYPES:
- person: Named individuals (not generic roles)
- organization: Companies, institutions, groups
- place: Locations, addresses, venues
- event: Meetings, appointments, activities with time
- thing: Products, objects, concepts mentioned

RELATIONSHIP TYPES:
- works_at: Employment relationship
- lives_in: Residence
- knows: Personal connection
- attended: Participated in event
- located_at: Place within place
- part_of: Membership or inclusion
- related_to: General association

EXTRACTION RULES:
1. Only extract NAMED entities (not "my friend" but "John")
2. Use "speaker" as the subject when the user mentions themselves
3. Extract temporal info for events (dates, times)
4. Capture promises/commitments with deadlines
5. Skip filler words, small talk, and vague references
6. Normalize names (capitalize properly)
7. Assign appropriate emoji icons to entities

Return a JSON object with this structure:
{
  "entities": [
    {
      "name": "Entity Name",
      "type": "person|organization|place|event|thing",
      "details": "Brief description or context",
      "icon": "Appropriate emoji",
      "when": "Time reference for events (optional)"
    }
  ],
  "relationships": [
    {
      "subject": "Entity name or 'speaker'",
      "relation": "works_at|lives_in|knows|attended|located_at|part_of|related_to",
      "object": "Target entity name"
    }
  ],
  "promises": [
    {
      "action": "What was promised",
      "to": "Person promised to (optional)",
      "deadline": "When it should be done (optional)"
    }
  ]
}

If no entities, relationships, or promises are found, return empty arrays.
Only return valid JSON, no additional text."""


def _get_llm_client():
    """Get async OpenAI client from model registry."""
    registry = get_models_registry()
    if not registry:
        raise RuntimeError("Model registry not configured")

    llm_def = registry.get_default("llm")
    if not llm_def:
        raise RuntimeError("No default LLM defined in config.yml")

    return create_openai_client(
        api_key=llm_def.api_key or "",
        base_url=llm_def.model_url,
        is_async=True,
    ), llm_def


async def extract_entities_from_transcript(
    transcript: str,
    conversation_id: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> ExtractionResult:
    """Extract entities, relationships, and promises from a transcript.

    Args:
        transcript: The conversation transcript text
        conversation_id: Optional ID of the source conversation
        custom_prompt: Optional custom prompt to override default

    Returns:
        ExtractionResult containing extracted entities, relationships, and promises
    """
    if not transcript or not transcript.strip():
        logger.debug("Empty transcript, returning empty extraction result")
        return ExtractionResult()

    try:
        from advanced_omi_backend.prompt_registry import get_prompt_registry

        client, llm_def = _get_llm_client()
        if custom_prompt:
            prompt = custom_prompt
        else:
            registry = get_prompt_registry()
            prompt = await registry.get_prompt("knowledge_graph.entity_extraction")

        response = await client.chat.completions.create(
            model=llm_def.model_name,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript},
            ],
            temperature=0.1,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )

        content = (response.choices[0].message.content or "").strip()
        if not content:
            logger.warning("LLM returned empty response for entity extraction")
            return ExtractionResult()

        return _parse_extraction_response(content)

    except Exception as e:
        logger.error(f"Entity extraction failed: {e}")
        return ExtractionResult()


def _parse_extraction_response(content: str) -> ExtractionResult:
    """Parse LLM response into ExtractionResult.

    Args:
        content: JSON string from LLM response

    Returns:
        Parsed ExtractionResult
    """
    try:
        # Handle potential </think> tags from some models
        if "</think>" in content:
            content = content.split("</think>", 1)[1].strip()

        data = json.loads(content)

        # Parse entities
        entities = []
        for e in data.get("entities", []):
            if isinstance(e, dict) and e.get("name"):
                entity_type = _normalize_entity_type(e.get("type", "thing"))
                entities.append(ExtractedEntity(
                    name=e["name"].strip(),
                    type=entity_type,
                    details=e.get("details"),
                    icon=e.get("icon") or _get_default_icon(entity_type),
                    when=e.get("when"),
                ))

        # Parse relationships
        relationships = []
        for r in data.get("relationships", []):
            if isinstance(r, dict) and r.get("subject") and r.get("object"):
                relationships.append(ExtractedRelationship(
                    subject=r["subject"].strip(),
                    relation=_normalize_relation_type(r.get("relation", "related_to")),
                    object=r["object"].strip(),
                ))

        # Parse promises
        promises = []
        for p in data.get("promises", []):
            if isinstance(p, dict) and p.get("action"):
                promises.append(ExtractedPromise(
                    action=p["action"].strip(),
                    to=p.get("to"),
                    deadline=p.get("deadline"),
                ))

        logger.info(f"Extracted {len(entities)} entities, {len(relationships)} relationships, {len(promises)} promises")
        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            promises=promises,
        )

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse extraction JSON: {e}")
        return ExtractionResult()
    except Exception as e:
        logger.error(f"Error parsing extraction response: {e}")
        return ExtractionResult()


def _normalize_entity_type(type_str: str) -> str:
    """Normalize entity type string to valid EntityType value."""
    type_lower = type_str.lower().strip()
    type_map = {
        "person": "person",
        "people": "person",
        "individual": "person",
        "organization": "organization",
        "company": "organization",
        "org": "organization",
        "institution": "organization",
        "place": "place",
        "location": "place",
        "venue": "place",
        "address": "place",
        "event": "event",
        "meeting": "event",
        "appointment": "event",
        "activity": "event",
        "thing": "thing",
        "object": "thing",
        "product": "thing",
        "concept": "thing",
    }
    return type_map.get(type_lower, "thing")


def _normalize_relation_type(relation_str: str) -> str:
    """Normalize relationship type string."""
    relation_lower = relation_str.lower().strip().replace(" ", "_").replace("-", "_")
    relation_map = {
        "works_at": "works_at",
        "employed_at": "works_at",
        "works_for": "works_at",
        "lives_in": "lives_in",
        "resides_in": "lives_in",
        "knows": "knows",
        "met": "knows",
        "friends_with": "knows",
        "attended": "attended",
        "participated_in": "attended",
        "went_to": "attended",
        "located_at": "located_at",
        "in": "located_at",
        "at": "located_at",
        "part_of": "part_of",
        "member_of": "part_of",
        "belongs_to": "part_of",
        "related_to": "related_to",
        "associated_with": "related_to",
        "connected_to": "related_to",
    }
    return relation_map.get(relation_lower, "related_to")


def _get_default_icon(entity_type: str) -> str:
    """Get default emoji icon for entity type."""
    icons = {
        "person": "👤",
        "organization": "🏢",
        "place": "📍",
        "event": "📅",
        "thing": "📦",
        "conversation": "💬",
        "promise": "✅",
        "fact": "💡",
    }
    return icons.get(entity_type, "📌")


def parse_natural_datetime(text: Optional[str], reference_date: Optional[datetime] = None) -> Optional[datetime]:
    """Parse natural language date/time into datetime.

    Args:
        text: Natural language time reference (e.g., "next Tuesday 2pm", "Friday")
        reference_date: Reference date for relative times (defaults to now)

    Returns:
        Parsed datetime or None if parsing fails
    """
    if not text:
        return None

    reference = reference_date or datetime.utcnow()
    text_lower = text.lower().strip()

    # Simple patterns - can be extended with dateparser library later
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    }

    try:
        # Handle "next [weekday]"
        for day_name, day_num in weekdays.items():
            if day_name in text_lower:
                days_ahead = day_num - reference.weekday()
                if days_ahead <= 0:  # Target day already happened this week
                    days_ahead += 7
                if "next" in text_lower:
                    days_ahead += 7  # "next" means the following week
                return reference + timedelta(days=days_ahead)

        # Handle "tomorrow"
        if "tomorrow" in text_lower:
            return reference + timedelta(days=1)

        # Handle "today"
        if "today" in text_lower:
            return reference

        # Handle "in X days/weeks"
        import re
        match = re.search(r"in (\d+) (day|week|month)s?", text_lower)
        if match:
            num = int(match.group(1))
            unit = match.group(2)
            if unit == "day":
                return reference + timedelta(days=num)
            elif unit == "week":
                return reference + timedelta(weeks=num)
            elif unit == "month":
                return reference + timedelta(days=num * 30)  # Approximate

        return None

    except Exception as e:
        logger.debug(f"Could not parse datetime '{text}': {e}")
        return None
