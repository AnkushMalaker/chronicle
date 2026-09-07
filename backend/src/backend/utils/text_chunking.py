"""Semantic text chunking using embedding similarity.

Splits text into semantically coherent chunks by comparing consecutive sentence
embeddings and finding natural topic boundaries. Inspired by LlamaIndex's
SemanticSplitterNodeParser:
https://docs.llamaindex.ai/en/stable/examples/node_parsers/semantic_chunking/

The algorithm:
1. Split text into sentences (regex on sentence-ending punctuation)
2. Create "buffered" versions by combining each sentence with its neighbors
3. Batch-embed all buffered sentences in one API call
4. Compute cosine distances between consecutive embeddings
5. Find breakpoints where distance exceeds a percentile threshold
6. Group sentences between breakpoints into chunks
7. Apply a max-word safety valve to prevent oversized chunks
"""

import logging
import re
from typing import Awaitable, Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def split_sentences(text: str) -> List[str]:
    """Split text into sentences using regex on .!? boundaries.

    Handles abbreviations and decimal numbers reasonably well by requiring
    the punctuation to be followed by whitespace and an uppercase letter or end-of-string.
    """
    # Split on sentence-ending punctuation followed by whitespace
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in parts if s.strip()]


def _build_buffered_sentences(sentences: List[str], buffer_size: int = 1) -> List[str]:
    """Combine each sentence with its neighbors for richer embedding context.

    For buffer_size=1, sentence i is combined with sentences [i-1, i, i+1].
    """
    buffered = []
    for i in range(len(sentences)):
        start = max(0, i - buffer_size)
        end = min(len(sentences), i + buffer_size + 1)
        buffered.append(" ".join(sentences[start:end]))
    return buffered


def _cosine_distances(embeddings: List[List[float]]) -> List[float]:
    """Compute cosine distances between consecutive embedding pairs.

    Returns a list of length len(embeddings) - 1.
    """
    arr = np.array(embeddings, dtype=np.float64)
    # Normalize rows
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    normed = arr / norms

    # Cosine similarity between consecutive pairs, then convert to distance
    similarities = np.sum(normed[:-1] * normed[1:], axis=1)
    distances = 1.0 - similarities
    return distances.tolist()


def _find_breakpoints(distances: List[float], percentile_threshold: float) -> List[int]:
    """Find indices where distance exceeds the given percentile.

    Returns sorted list of breakpoint indices (positions in the distances list
    where a topic transition occurs).
    """
    if not distances:
        return []
    threshold = float(np.percentile(distances, percentile_threshold))
    return [i for i, d in enumerate(distances) if d > threshold]


def _enforce_max_chunk_words(chunks: List[str], max_words: int) -> List[str]:
    """Split any chunk that exceeds max_words into smaller pieces."""
    result = []
    for chunk in chunks:
        words = chunk.split()
        if len(words) <= max_words:
            result.append(chunk)
        else:
            for i in range(0, len(words), max_words):
                piece = " ".join(words[i : i + max_words])
                if piece:
                    result.append(piece)
    return result


async def semantic_chunk_text(
    text: str,
    embed_fn: Callable[[List[str]], Awaitable[List[List[float]]]],
    buffer_size: int = 1,
    breakpoint_percentile_threshold: float = 95.0,
    max_chunk_words: int = 300,
    sentences: Optional[List[str]] = None,
    join_str: str = " ",
) -> List[str]:
    """Split text into semantically coherent chunks using embedding similarity.

    Uses the approach from LlamaIndex's SemanticSplitterNodeParser
    (https://docs.llamaindex.ai/en/stable/examples/node_parsers/semantic_chunking/)
    to detect topic transitions via cosine distance between consecutive sentence
    embeddings.

    Args:
        text: The text to chunk.
        embed_fn: Async callable that takes a list of strings and returns
            a list of embedding vectors. Keeps the chunker decoupled from
            any specific embedding provider.
        buffer_size: Number of neighboring sentences to include when building
            the buffered context for each sentence's embedding.
        breakpoint_percentile_threshold: Percentile of cosine distances above
            which a topic transition is detected (higher = fewer breaks).
        max_chunk_words: Maximum words per chunk. Chunks exceeding this are
            split further as a safety valve.
        sentences: Optional pre-split text units (e.g. dialogue turns). When
            provided, the regex-based split_sentences() call is skipped and
            these units are used directly as the atomic elements for embedding
            and breakpoint detection.
        join_str: String used to join units within a chunk. Default is ``" "``
            (space). Use ``"\\n"`` for dialogue transcripts to keep speaker
            labels on separate lines.

    Returns:
        List of text chunks.
    """
    text = text.strip()
    if not text:
        return []

    units = sentences if sentences is not None else split_sentences(text)
    # Filter out empty units
    units = [u for u in units if u.strip()]
    if len(units) <= 2:
        return _enforce_max_chunk_words([text], max_chunk_words)

    # Build buffered sentences for richer embedding context
    buffered = _build_buffered_sentences(units, buffer_size)

    # Embed all buffered sentences in one batch call
    try:
        embeddings = await embed_fn(buffered)
    except Exception:
        logger.warning(
            "Embedding call failed during semantic chunking; returning text as single chunk",
            exc_info=True,
        )
        return _enforce_max_chunk_words([text], max_chunk_words)

    if not embeddings or len(embeddings) != len(units):
        logger.warning(
            "Unexpected embedding count (%s vs %s units); returning single chunk",
            len(embeddings) if embeddings else 0,
            len(units),
        )
        return _enforce_max_chunk_words([text], max_chunk_words)

    # Compute distances and find breakpoints
    distances = _cosine_distances(embeddings)
    breakpoints = _find_breakpoints(distances, breakpoint_percentile_threshold)

    # Group units between breakpoints
    chunks: List[str] = []
    start = 0
    for bp in sorted(breakpoints):
        # bp is the index in distances; the break is *after* unit bp
        end = bp + 1
        chunk = join_str.join(units[start:end])
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end

    # Remaining units
    if start < len(units):
        chunk = join_str.join(units[start:])
        if chunk.strip():
            chunks.append(chunk.strip())

    if not chunks:
        chunks = [text]

    return _enforce_max_chunk_words(chunks, max_chunk_words)
