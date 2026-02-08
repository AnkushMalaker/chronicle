
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal, Optional, Union

Event = Literal["ADD", "UPDATE", "DELETE", "NONE"]
NUMERIC_ID = re.compile(r"^\d+$")
ALLOWED_EVENTS = {"ADD", "UPDATE", "DELETE", "NONE"}

@dataclass(frozen=True)
class MemoryItem:
    id: str
    event: Event
    text: str
    old_memory: Optional[str] = None

class MemoryXMLParseError(ValueError):
    pass

def extract_xml_from_content(content: str) -> str:
    """
    Extract XML from content that might contain other text.
    Looks for content between <result> and </result> tags.
    """
    # Try to find XML block within the content
    import re

    # Look for <result>...</result> block
    xml_match = re.search(r'<result>.*?</result>', content, re.DOTALL)
    if xml_match:
        return xml_match.group(0)
    
    # If no <result> tags found, return the original content
    return content

def clean_and_validate_xml(xml_str: str) -> str:
    """
    Clean common XML issues and validate structure.
    """
    xml_str = xml_str.strip()
    
    # Print raw XML for debugging
    print("Raw XML content:")
    print("=" * 50)
    print(repr(xml_str))
    print("=" * 50)
    print("Formatted XML content:")
    lines = xml_str.split('\n')
    for i, line in enumerate(lines, 1):
        print(f"{i:2d}: {line}")
    print("=" * 50)
    
    return xml_str

def extract_assistant_xml_from_openai_response(response) -> str:
    """
    Extract XML content from OpenAI ChatCompletion response.
    Works with both OpenAI API and Ollama via OpenAI-compatible endpoint.
    """
    try:
        # OpenAI ChatCompletion object structure
        return response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as e:
        raise MemoryXMLParseError(f"Could not extract assistant XML from OpenAI response: {e}") from e

def parse_memory_xml(xml_str: str) -> List[MemoryItem]:
    """
    Parse and validate the memory XML.

    Changes from your original:
    - UPDATE items no longer *require* <old_memory>. If missing, old_memory=None.
    - <old_memory> is still forbidden for non-UPDATE events.
    """
    # First extract XML if it's embedded in other content
    xml_str = extract_xml_from_content(xml_str)

    # Clean and validate
    xml_str = clean_and_validate_xml(xml_str)

    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError as e:
        print(f"\nXML Parse Error: {e}")
        print("This usually means:")
        print("- Unclosed tags (e.g., <item> without </item>)")
        print("- Mismatched tags (e.g., <item> closed with </memory>)")
        print("- Invalid characters in XML")
        print("- Missing quotes around attribute values")
        raise MemoryXMLParseError(f"Invalid XML: {e}") from e

    if root.tag != "result":
        raise MemoryXMLParseError("Root element must be <result>.")

    memory = root.find("memory")
    if memory is None:
        raise MemoryXMLParseError("<memory> section is required.")

    items: List[MemoryItem] = []
    seen_ids = set()

    for item in memory.findall("item"):
        # Attributes
        item_id = item.get("id")
        event = item.get("event")

        if not item_id:
            raise MemoryXMLParseError("<item> is missing required 'id' attribute.")
        if not NUMERIC_ID.match(item_id):
            raise MemoryXMLParseError(f"id must be numeric: {item_id!r}")
        if item_id in seen_ids:
            raise MemoryXMLParseError(f"Duplicate id detected: {item_id}")
        seen_ids.add(item_id)

        if event not in ALLOWED_EVENTS:
            raise MemoryXMLParseError(f"Invalid event {event!r} for id {item_id}.")

        # Children
        text_el = item.find("text")
        if text_el is None or (text_el.text or "").strip() == "":
            raise MemoryXMLParseError(f"<text> is required and non-empty for id {item_id}.")
        text_val = (text_el.text or "").strip()
        
        # No JSON expansion needed - individual facts are now properly handled by improved prompts

        old_el = item.find("old_memory")
        old_val = (old_el.text or "").strip() if old_el is not None else None

        # Event-specific validation
        if event == "UPDATE":
            # ALLOW missing/empty <old_memory>; just keep None if not present
            pass
        else:
            # For non-UPDATE, <old_memory> must not appear
            if old_el is not None:
                raise MemoryXMLParseError(f"<old_memory> must only appear for UPDATE (id {item_id}).")

        items.append(MemoryItem(id=item_id, event=event, text=text_val, old_memory=old_val))

    if not items:
        raise MemoryXMLParseError("No <item> elements found in <memory>.")

    return items


def items_to_json(items: List[MemoryItem]) -> Dict[str, Any]:
    """Convert parsed items to JSON; only include old_memory when present."""
    out: List[Dict[str, Any]] = []
    for it in items:
        obj: Dict[str, Any] = {"id": it.id, "event": it.event, "text": it.text}
        if it.event == "UPDATE" and it.old_memory:  # include only if non-empty
            obj["old_memory"] = it.old_memory
        out.append(obj)
    return {"memory": out}