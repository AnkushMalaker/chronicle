"""Unit tests for semantic text chunking."""

import asyncio
import math
import os
import sys
import unittest
from unittest.mock import AsyncMock

import numpy as np

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from advanced_omi_backend.utils.text_chunking import (
    _build_buffered_sentences,
    _cosine_distances,
    _enforce_max_chunk_words,
    _find_breakpoints,
    semantic_chunk_text,
    split_sentences,
)


class TestSplitSentences(unittest.TestCase):
    def test_basic_splitting(self):
        text = "Hello world. How are you? I am fine!"
        result = split_sentences(text)
        self.assertEqual(result, ["Hello world.", "How are you?", "I am fine!"])

    def test_single_sentence(self):
        self.assertEqual(split_sentences("Just one sentence."), ["Just one sentence."])

    def test_empty_string(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])

    def test_no_terminal_punctuation(self):
        result = split_sentences("No punctuation here")
        self.assertEqual(result, ["No punctuation here"])

    def test_multiple_spaces(self):
        result = split_sentences("First sentence.   Second sentence.")
        self.assertEqual(len(result), 2)

    def test_newlines_split_sentences(self):
        result = split_sentences("Hello world.\nNew line here.")
        # Newline after punctuation splits into separate sentences
        self.assertEqual(len(result), 2)

    def test_preserves_sentence_content(self):
        text = "The temperature is 3.5 degrees. It is cold."
        result = split_sentences(text)
        self.assertEqual(len(result), 2)


class TestBuildBufferedSentences(unittest.TestCase):
    def test_buffer_size_zero(self):
        sentences = ["A.", "B.", "C."]
        result = _build_buffered_sentences(sentences, buffer_size=0)
        self.assertEqual(result, ["A.", "B.", "C."])

    def test_buffer_size_one(self):
        sentences = ["A.", "B.", "C.", "D."]
        result = _build_buffered_sentences(sentences, buffer_size=1)
        self.assertEqual(result[0], "A. B.")  # [0:2]
        self.assertEqual(result[1], "A. B. C.")  # [0:3]
        self.assertEqual(result[2], "B. C. D.")  # [1:4]
        self.assertEqual(result[3], "C. D.")  # [2:4]

    def test_single_sentence(self):
        result = _build_buffered_sentences(["Only one."], buffer_size=1)
        self.assertEqual(result, ["Only one."])


class TestCosineDistances(unittest.TestCase):
    def test_identical_vectors(self):
        embeddings = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        distances = _cosine_distances(embeddings)
        self.assertEqual(len(distances), 2)
        for d in distances:
            self.assertAlmostEqual(d, 0.0, places=6)

    def test_orthogonal_vectors(self):
        embeddings = [[1.0, 0.0], [0.0, 1.0]]
        distances = _cosine_distances(embeddings)
        self.assertAlmostEqual(distances[0], 1.0, places=6)

    def test_opposite_vectors(self):
        embeddings = [[1.0, 0.0], [-1.0, 0.0]]
        distances = _cosine_distances(embeddings)
        self.assertAlmostEqual(distances[0], 2.0, places=6)

    def test_known_values(self):
        # Two similar, then one different
        embeddings = [[1.0, 0.0], [0.95, 0.05], [0.0, 1.0]]
        distances = _cosine_distances(embeddings)
        self.assertEqual(len(distances), 2)
        # First pair should be close (small distance)
        self.assertLess(distances[0], 0.1)
        # Second pair should be far (large distance)
        self.assertGreater(distances[1], 0.5)

    def test_zero_vector_handling(self):
        embeddings = [[0.0, 0.0], [1.0, 0.0]]
        distances = _cosine_distances(embeddings)
        # Zero vector gets norm=1 (no division by zero)
        self.assertEqual(len(distances), 1)


class TestFindBreakpoints(unittest.TestCase):
    def test_clear_breakpoint(self):
        # Low distances except one spike
        distances = [0.01, 0.02, 0.01, 0.9, 0.01, 0.02]
        breakpoints = _find_breakpoints(distances, 90.0)
        self.assertIn(3, breakpoints)

    def test_no_breakpoints_uniform(self):
        distances = [0.1, 0.1, 0.1, 0.1]
        breakpoints = _find_breakpoints(distances, 95.0)
        # With all equal distances, the 95th percentile = 0.1, and we need > threshold
        self.assertEqual(breakpoints, [])

    def test_empty_distances(self):
        self.assertEqual(_find_breakpoints([], 95.0), [])

    def test_single_distance(self):
        breakpoints = _find_breakpoints([0.5], 50.0)
        # 50th percentile of [0.5] = 0.5; nothing is > 0.5
        self.assertEqual(breakpoints, [])


class TestEnforceMaxChunkWords(unittest.TestCase):
    def test_no_split_needed(self):
        chunks = ["short chunk", "another one"]
        result = _enforce_max_chunk_words(chunks, max_words=10)
        self.assertEqual(result, chunks)

    def test_split_long_chunk(self):
        long_chunk = " ".join(f"word{i}" for i in range(20))
        result = _enforce_max_chunk_words([long_chunk], max_words=10)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0].split()), 10)
        self.assertEqual(len(result[1].split()), 10)

    def test_empty_chunks(self):
        self.assertEqual(_enforce_max_chunk_words([], max_words=10), [])


class TestSemanticChunkText(unittest.TestCase):
    def test_empty_text(self):
        embed_fn = AsyncMock()
        result = asyncio.run(semantic_chunk_text("", embed_fn))
        self.assertEqual(result, [])
        embed_fn.assert_not_awaited()

    def test_single_sentence_returns_whole_text(self):
        embed_fn = AsyncMock()
        result = asyncio.run(semantic_chunk_text("Just one sentence.", embed_fn))
        self.assertEqual(result, ["Just one sentence."])
        embed_fn.assert_not_awaited()

    def test_two_sentences_returns_whole_text(self):
        embed_fn = AsyncMock()
        text = "First sentence. Second sentence."
        result = asyncio.run(semantic_chunk_text(text, embed_fn))
        self.assertEqual(result, [text])
        embed_fn.assert_not_awaited()

    def test_topic_transition_detected(self):
        """Three sentences: first two similar, third different. Should split."""

        async def mock_embed(texts):
            embeddings = []
            for t in texts:
                if "weather" in t.lower():
                    embeddings.append([1.0, 0.0, 0.0])
                else:
                    embeddings.append([0.0, 0.0, 1.0])
            return embeddings

        text = (
            "The weather is nice. It is sunny today. Python is a programming language."
        )
        result = asyncio.run(
            semantic_chunk_text(text, mock_embed, breakpoint_percentile_threshold=50.0)
        )
        # Should detect the topic transition
        self.assertGreater(len(result), 1)

    def test_uniform_topic_single_chunk(self):
        """All sentences on the same topic should stay together."""

        async def mock_embed(texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

        text = "Dogs are great. Dogs are loyal. Dogs are friendly."
        result = asyncio.run(semantic_chunk_text(text, mock_embed))
        self.assertEqual(len(result), 1)

    def test_embed_fn_failure_returns_single_chunk(self):
        """If embedding fails, fall back to returning text as single chunk."""

        async def failing_embed(texts):
            raise RuntimeError("API error")

        text = "First sentence. Second sentence. Third sentence."
        result = asyncio.run(semantic_chunk_text(text, failing_embed))
        self.assertEqual(result, [text])

    def test_max_chunk_words_applied(self):
        """Long uniform text should still be split by max_chunk_words."""
        words = " ".join(f"word{i}." for i in range(100))

        async def mock_embed(texts):
            return [[1.0, 0.0]] * len(texts)

        result = asyncio.run(semantic_chunk_text(words, mock_embed, max_chunk_words=30))
        for chunk in result:
            self.assertLessEqual(len(chunk.split()), 30)

    def test_wrong_embedding_count_returns_single_chunk(self):
        """If embed_fn returns wrong number of embeddings, fall back gracefully."""

        async def wrong_count_embed(texts):
            return [[1.0, 0.0]]  # Always returns 1 regardless of input

        text = "First sentence. Second sentence. Third sentence."
        result = asyncio.run(semantic_chunk_text(text, wrong_count_embed))
        self.assertEqual(result, [text])


class TestSemanticChunkTextWithSentences(unittest.TestCase):
    """Tests for the `sentences` and `join_str` parameters."""

    def test_sentences_param_skips_split(self):
        """Pre-split units should be used directly, not regex-split."""
        call_count = {"n": 0}

        async def mock_embed(texts):
            call_count["n"] += 1
            # Return distinct embeddings so we can verify units are passed through
            embeddings = []
            for i, _ in enumerate(texts):
                vec = [0.0] * 3
                vec[i % 3] = 1.0
                embeddings.append(vec)
            return embeddings

        # These dialogue turns have no sentence-ending punctuation — regex
        # split_sentences would return them as a single unit.
        turns = [
            "Alice: Hey how are you",
            "Bob: I'm good thanks",
            "Alice: Want to grab lunch",
            "Bob: Sure let's go",
        ]
        text = "\n".join(turns)
        result = asyncio.run(
            semantic_chunk_text(
                text, mock_embed, sentences=turns, breakpoint_percentile_threshold=50.0
            )
        )
        # embed_fn should have been called (4 units > 2 threshold)
        self.assertEqual(call_count["n"], 1)
        # Result should contain all turns (possibly grouped)
        joined = " ".join(result)
        for turn in turns:
            self.assertIn(turn, joined)

    def test_join_str_newline_preserves_dialogue(self):
        """With join_str='\\n', chunks should keep speaker labels on separate lines."""

        async def same_topic_embed(texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

        turns = [
            "Alice: The project is on track",
            "Bob: Great to hear",
            "Alice: We should ship next week",
        ]
        text = "\n".join(turns)
        result = asyncio.run(
            semantic_chunk_text(text, same_topic_embed, sentences=turns, join_str="\n")
        )
        # All same topic → single chunk with newlines
        self.assertEqual(len(result), 1)
        self.assertIn("\n", result[0])
        # Each turn should be on its own line
        lines = result[0].split("\n")
        self.assertEqual(len(lines), 3)

    def test_single_turn_returns_whole_text(self):
        """A single dialogue turn should return the full text."""
        embed_fn = AsyncMock()
        turns = ["Alice: Hello"]
        text = "Alice: Hello"
        result = asyncio.run(semantic_chunk_text(text, embed_fn, sentences=turns))
        self.assertEqual(result, [text])
        embed_fn.assert_not_awaited()

    def test_two_turns_returns_whole_text(self):
        """Two dialogue turns should return the full text (below threshold)."""
        embed_fn = AsyncMock()
        turns = ["Alice: Hello", "Bob: Hi"]
        text = "\n".join(turns)
        result = asyncio.run(semantic_chunk_text(text, embed_fn, sentences=turns))
        self.assertEqual(result, [text])
        embed_fn.assert_not_awaited()

    def test_topic_transition_with_dialogue(self):
        """Dialogue that switches topics should be split into separate chunks."""

        async def mock_embed(texts):
            embeddings = []
            for t in texts:
                if "weather" in t.lower() or "sunny" in t.lower():
                    embeddings.append([1.0, 0.0, 0.0])
                else:
                    embeddings.append([0.0, 0.0, 1.0])
            return embeddings

        turns = [
            "Alice: The weather is beautiful today",
            "Bob: Yes it's very sunny outside",
            "Alice: By the way I started learning Python",
            "Bob: Oh that's a great programming language",
        ]
        text = "\n".join(turns)
        result = asyncio.run(
            semantic_chunk_text(
                text,
                mock_embed,
                sentences=turns,
                join_str="\n",
                breakpoint_percentile_threshold=50.0,
            )
        )
        self.assertGreater(len(result), 1)

    def test_empty_turns_filtered(self):
        """Empty strings in sentences list should be filtered out."""
        embed_fn = AsyncMock()
        turns = ["Alice: Hello", "", "  ", "Bob: Hi"]
        text = "Alice: Hello\nBob: Hi"
        result = asyncio.run(semantic_chunk_text(text, embed_fn, sentences=turns))
        # After filtering: 2 units → returns whole text
        self.assertEqual(result, [text])
        embed_fn.assert_not_awaited()

    def test_max_chunk_words_still_applied(self):
        """The max_chunk_words safety valve should apply to dialogue chunks."""

        async def same_topic(texts):
            return [[1.0, 0.0]] * len(texts)

        # Each turn has ~10 words; 5 turns = ~50 words
        turns = [
            f"Speaker: word {i} " + " ".join(f"w{j}" for j in range(8))
            for i in range(5)
        ]
        text = "\n".join(turns)
        result = asyncio.run(
            semantic_chunk_text(
                text,
                same_topic,
                sentences=turns,
                join_str="\n",
                max_chunk_words=20,
            )
        )
        for chunk in result:
            self.assertLessEqual(len(chunk.split()), 20)


if __name__ == "__main__":
    unittest.main()
