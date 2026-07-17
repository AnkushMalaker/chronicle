"""Shared utilities for FalkorDB graph memory validation scripts."""

import asyncio
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from falkordb import FalkorDB

# Load .env from backends/advanced/
# __file__ is .../backends/advanced/tests/scripts/graph-validation/utils.py
# parents: [0]=graph-validation, [1]=scripts, [2]=tests, [3]=advanced
_env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_env_path)

# --- FalkorDB Connection ---

_falkordb_host = os.getenv("FALKORDB_HOST", "localhost")
if _falkordb_host == "falkordb":
    _falkordb_host = "localhost"
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6381"))
FALKORDB_GRAPH = os.getenv("FALKORDB_GRAPH", "chronicle")

# --- OpenAI Embeddings ---

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


def get_graph(host=None, port=None, graph_name=None):
    """Create a FalkorDB graph connected to the local instance."""
    host = host or _falkordb_host
    port = port or FALKORDB_PORT
    graph_name = graph_name or FALKORDB_GRAPH
    db = FalkorDB(host=host, port=port)
    return db.select_graph(graph_name)


async def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Generate embeddings using OpenAI API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    response = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [data.embedding for data in response.data]


def generate_embeddings_sync(texts: List[str]) -> List[List[float]]:
    """Synchronous wrapper for embedding generation."""
    return asyncio.run(generate_embeddings(texts))


# --- Markdown Chunking ---


def split_on_headers(markdown: str) -> List[Dict[str, str]]:
    """Split markdown into chunks by ### headers.

    Returns list of dicts with 'section_title' and 'text' keys.
    Frontmatter (---...---) is stripped and returned as metadata.
    """
    # Strip frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown, re.DOTALL)
    if fm_match:
        content = markdown[fm_match.end() :]
    else:
        content = markdown

    chunks = []
    # Split on ### headers (h3)
    parts = re.split(r"^(###\s+.+)$", content, flags=re.MULTILINE)

    # parts alternates: [pre-header text, header, body, header, body, ...]
    # First element is text before any header
    i = 0
    if parts[0].strip():
        chunks.append({"section_title": "Introduction", "text": parts[0].strip()})
        i = 1
    else:
        i = 1

    while i < len(parts):
        if parts[i].startswith("###"):
            title = parts[i].replace("###", "").strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if body:
                chunks.append({"section_title": title, "text": body})
            i += 2
        else:
            i += 1

    return chunks


def parse_frontmatter(markdown: str) -> Dict[str, str]:
    """Extract YAML frontmatter as a simple dict."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown, re.DOTALL)
    if not fm_match:
        return {}
    result = {}
    for line in fm_match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


# --- Entity Parsing ---


def parse_people_section(markdown: str) -> List[Dict[str, str]]:
    """Parse the ### People section to extract entity names and descriptions.

    Expected format:
    ### People
    - John (coworker, project lead)
    - Dr. Sarah Chen (dentist, referred by Jane)
    - "The IT guy" (unnamed, fixed the printer)

    Returns list of dicts with 'name' and 'description'.
    """
    # Find the People section
    match = re.search(
        r"^###\s+People\s*\n(.*?)(?=^###|\Z)", markdown, re.MULTILINE | re.DOTALL
    )
    if not match:
        return []

    people = []
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()

        # Try to parse "Name (description)" format
        paren_match = re.match(r'^["""]?(.+?)["""]?\s*\((.+?)\)\s*$', line)
        if paren_match:
            people.append(
                {
                    "name": paren_match.group(1).strip(),
                    "description": paren_match.group(2).strip(),
                }
            )
        else:
            # Just a name with no description
            people.append({"name": line.strip('"').strip("'"), "description": ""})

    return people


def parse_action_items(markdown: str) -> List[Dict[str, str]]:
    """Parse ### Action Items section.

    Expected format:
    ### Action Items
    - [ ] Send Q3 report to John by Friday
    - [x] Book dentist follow-up

    Returns list of dicts with 'text' and 'done' (bool).
    """
    match = re.search(
        r"^###\s+Action Items\s*\n(.*?)(?=^###|\Z)", markdown, re.MULTILINE | re.DOTALL
    )
    if not match:
        return []

    items = []
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()

        done = False
        if line.startswith("[x]") or line.startswith("[X]"):
            done = True
            line = line[3:].strip()
        elif line.startswith("[ ]"):
            line = line[3:].strip()

        if line:
            items.append({"text": line, "done": done})

    return items


# --- Hybrid Search Scoring ---


def compute_hybrid_scores(
    vector_results: List[Dict],
    fulltext_results: List[Dict],
    vector_weight: float = 0.7,
    text_weight: float = 0.3,
    recency_half_life_days: float = 30.0,
    recency_floor: float = 0.5,
) -> List[Dict]:
    """Merge vector and full-text results with recency bias.

    Each result dict must have: 'chunk_id', 'score', 'date' (ISO string or datetime).
    Additional fields are preserved.
    """
    now = datetime.now(timezone.utc)
    merged: Dict[str, Dict] = {}

    for r in vector_results:
        cid = r["chunk_id"]
        merged[cid] = {**r, "vector_score": r["score"], "text_score": 0.0}

    for r in fulltext_results:
        cid = r["chunk_id"]
        if cid in merged:
            merged[cid]["text_score"] = r["score"]
        else:
            merged[cid] = {**r, "vector_score": 0.0, "text_score": r["score"]}

    results = []
    for entry in merged.values():
        # Parse date
        d = entry.get("date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("Z", "+00:00"))
        if d is None:
            d = now

        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)

        age_days = (now - d).total_seconds() / 86400.0

        relevance = (
            vector_weight * entry["vector_score"] + text_weight * entry["text_score"]
        )
        recency = max(
            recency_floor, math.exp(-0.693 * age_days / recency_half_life_days)
        )
        entry["relevance_score"] = relevance
        entry["recency_score"] = recency
        entry["final_score"] = relevance * recency
        results.append(entry)

    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results


# --- Pretty Printing ---

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_pass(msg: str):
    print(f"  {GREEN}PASS{RESET} {msg}")


def print_fail(msg: str):
    print(f"  {RED}FAIL{RESET} {msg}")


def print_header(msg: str):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}{msg}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")


def print_results_table(results: List[Dict], fields: List[str], max_text_len: int = 60):
    """Print results as a simple table."""
    if not results:
        print("  (no results)")
        return
    for i, r in enumerate(results):
        parts = []
        for f in fields:
            val = r.get(f, "")
            if isinstance(val, float):
                parts.append(f"{f}={val:.4f}")
            elif isinstance(val, str) and len(val) > max_text_len:
                parts.append(f"{f}={val[:max_text_len]}...")
            else:
                parts.append(f"{f}={val}")
        print(f"  [{i+1}] {', '.join(parts)}")
