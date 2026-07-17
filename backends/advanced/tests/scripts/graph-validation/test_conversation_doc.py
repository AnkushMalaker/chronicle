"""Validate LLM conversation document generation and markdown parsing.

Tests:
1. LLM produces a well-structured .md from a sample transcript
2. Parsed sections match expected structure (Summary, Key Facts, People, Action Items)
3. Entity extraction from People section is reliable
4. Header-based chunking produces correct splits

This directly tests the review's concern about entity parsing fragility.

Usage:
    uv run python tests/scripts/graph-validation/test_conversation_doc.py
"""

import asyncio
import json

from openai import AsyncOpenAI
from utils import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    parse_action_items,
    parse_frontmatter,
    parse_people_section,
    print_fail,
    print_header,
    print_pass,
    split_on_headers,
)

SAMPLE_TRANSCRIPT = """Speaker 0: Hey John, thanks for meeting. I wanted to go over the Q3 timeline.
John: Sure, so the main concern is the backend migration. It might block our September 15th deadline.
Speaker 0: Right. Sarah mentioned she wants to descope the auth rewrite. What do you think?
John: I agree with Sarah actually. The auth rewrite is nice to have but not critical for Q3. Let's push it to Q4.
Speaker 0: Makes sense. I'll schedule a meeting with Sarah to align on scope. Oh and John, do you prefer morning or afternoon standups?
John: Morning for sure. I'm much more productive before noon.
Speaker 0: Got it. One more thing - we need the budget review ready by Friday. Can you pull the numbers?
John: Yeah I'll have the draft ready by Thursday. Mike from finance wants to review it too.
Speaker 0: Perfect. I'll also check with the DevOps team about the migration timeline. Thanks John.
John: Sounds good, talk later."""

GENERATE_CONVERSATION_DOC_PROMPT = """\
You are generating a structured conversation document from a transcript.

Given a transcript with speaker labels, produce a markdown document with this EXACT structure:

---
conversation_id: {conversation_id}
date: {date}
speakers: [{speakers}]
duration_minutes: {duration}
---

## {Title - descriptive, 3-8 words}

### Summary
{2-3 sentence summary of what was discussed}

### Key Facts
{Bulleted list of specific facts, decisions, numbers, dates mentioned}

### People
{Bulleted list in format: - Name (role/relationship, context)}
Include ALL named individuals — speakers, people mentioned, people referenced.
If a speaker is identified by name (e.g., "John" not "Speaker 0"), they MUST appear here.
Do not include unnamed roles or generic labels like "Speaker 0".

### Action Items
{Bulleted list in format: - [ ] Action item description}
Use [x] for items already completed in the conversation.

Rules:
- Every ### section MUST be present, even if empty (use "- None" for empty sections)
- People section: ONLY named individuals, format MUST be "- Name (description)"
- Key Facts: be specific — include dates, numbers, names
- Action Items: use checkbox format [ ] or [x]
- Do NOT add any sections beyond the four listed above
"""


async def generate_doc(transcript: str) -> str:
    """Call LLM to generate a conversation document."""
    client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": GENERATE_CONVERSATION_DOC_PROMPT},
            {
                "role": "user",
                "content": f"Transcript:\n{transcript}\n\nMetadata:\n- conversation_id: test_conv_llm\n- date: 2026-03-18T10:00:00\n- speakers: Speaker 0, John\n- duration: 8 minutes",
            },
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def test_structure(doc: str):
    """Verify the generated doc has all required sections."""
    print_header("Test: Document Structure")

    required_sections = ["Summary", "Key Facts", "People", "Action Items"]
    chunks = split_on_headers(doc)
    found_sections = [c["section_title"] for c in chunks]

    print(f"  Found sections: {found_sections}")

    for section in required_sections:
        if section in found_sections:
            print_pass(f"Section '{section}' present")
        else:
            print_fail(f"Section '{section}' MISSING")

    # Check frontmatter
    fm = parse_frontmatter(doc)
    if fm.get("conversation_id"):
        print_pass(f"Frontmatter has conversation_id: {fm['conversation_id']}")
    else:
        print_fail("Frontmatter missing conversation_id")

    if fm.get("date"):
        print_pass(f"Frontmatter has date: {fm['date']}")
    else:
        print_fail("Frontmatter missing date")

    return chunks


def test_entity_parsing(doc: str):
    """Test that People section can be reliably parsed."""
    print_header("Test: Entity Parsing from People Section")

    people = parse_people_section(doc)
    print(f"  Parsed entities: {len(people)}")
    for p in people:
        print(f"    - {p['name']} ({p['description']})")

    # Expected entities from the transcript
    expected_names = {"John", "Sarah", "Mike"}

    found_names = {p["name"] for p in people}
    for name in expected_names:
        if name in found_names:
            print_pass(f"Entity '{name}' extracted")
        else:
            print_fail(f"Entity '{name}' NOT extracted (found: {found_names})")

    # Check that descriptions are non-empty
    has_descriptions = sum(1 for p in people if p["description"])
    if has_descriptions == len(people):
        print_pass(f"All {len(people)} entities have descriptions")
    elif has_descriptions > 0:
        print_pass(f"{has_descriptions}/{len(people)} entities have descriptions")
    else:
        print_fail("No entities have descriptions")

    # Check for unnamed entries (these should NOT appear)
    unnamed = [
        p for p in people if p["name"].lower().startswith(("speaker", "the ", "a "))
    ]
    if not unnamed:
        print_pass("No unnamed/generic entities (good)")
    else:
        print_fail(f"Found unnamed entities: {[p['name'] for p in unnamed]}")

    return people


def test_action_items(doc: str):
    """Test Action Items parsing."""
    print_header("Test: Action Items Parsing")

    items = parse_action_items(doc)
    print(f"  Parsed items: {len(items)}")
    for item in items:
        status = "[x]" if item["done"] else "[ ]"
        print(f"    {status} {item['text']}")

    if items:
        print_pass(f"Extracted {len(items)} action items")
    else:
        print_fail("No action items extracted")

    # Should have at least 2 action items from the transcript
    if len(items) >= 2:
        print_pass(f"At least 2 action items found (got {len(items)})")
    else:
        print_fail(f"Expected at least 2 action items, got {len(items)}")


def test_chunking(doc: str):
    """Test header-based chunking produces correct splits."""
    print_header("Test: Header-Based Chunking")

    chunks = split_on_headers(doc)
    print(f"  Chunks: {len(chunks)}")
    for c in chunks:
        text_preview = c["text"][:80].replace("\n", " ")
        print(f"    [{c['section_title']}] {text_preview}...")

    # Should have 4 chunks (Summary, Key Facts, People, Action Items)
    if len(chunks) >= 4:
        print_pass(f"Got {len(chunks)} chunks (expected ≥4)")
    else:
        print_fail(f"Got {len(chunks)} chunks (expected ≥4)")

    # Each chunk should have non-empty text
    empty_chunks = [c for c in chunks if not c["text"].strip()]
    if not empty_chunks:
        print_pass("All chunks have non-empty text")
    else:
        print_fail(f"{len(empty_chunks)} chunks have empty text")


async def main():
    print_header("Generating Conversation Document via LLM")
    print(f"  Transcript length: {len(SAMPLE_TRANSCRIPT)} chars")
    print(f"  Generating...")

    doc = await generate_doc(SAMPLE_TRANSCRIPT)

    print(f"  Generated document: {len(doc)} chars")
    print(f"\n{'─'*60}")
    print(doc)
    print(f"{'─'*60}\n")

    # Run all tests
    test_structure(doc)
    test_entity_parsing(doc)
    test_action_items(doc)
    test_chunking(doc)


if __name__ == "__main__":
    asyncio.run(main())
