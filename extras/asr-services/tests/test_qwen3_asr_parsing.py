"""
Tests for Qwen3-ASR output parsing and repetition detection.

Pure function tests — no GPU, no vLLM, no network required.

Run:
    cd extras/asr-services
    uv run pytest tests/test_qwen3_asr_parsing.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.qwen3_asr.transcriber import (
    _parse_qwen3_output,
    detect_and_fix_repetitions,
)

# ---------------------------------------------------------------------------
# _parse_qwen3_output tests
# ---------------------------------------------------------------------------


class TestParseQwen3Output:
    """Tests for _parse_qwen3_output(raw) → (language, text)."""

    def test_standard_english(self):
        lang, text = _parse_qwen3_output(
            "language English<asr_text>hello world</asr_text>"
        )
        assert lang == "English"
        assert text == "hello world"

    def test_standard_chinese(self):
        lang, text = _parse_qwen3_output(
            "language Chinese<asr_text>你好世界</asr_text>"
        )
        assert lang == "Chinese"
        assert text == "你好世界"

    def test_silent_audio_language_none(self):
        lang, text = _parse_qwen3_output("language None<asr_text></asr_text>")
        assert lang == ""
        assert text == ""

    def test_silent_with_unexpected_text(self):
        lang, text = _parse_qwen3_output("language None<asr_text>hmm</asr_text>")
        assert lang == ""
        assert text == "hmm"

    def test_plain_text_no_tags(self):
        lang, text = _parse_qwen3_output("just plain text")
        assert lang == ""
        assert text == "just plain text"

    def test_empty_string(self):
        lang, text = _parse_qwen3_output("")
        assert lang == ""
        assert text == ""

    def test_none_input(self):
        lang, text = _parse_qwen3_output(None)
        assert lang == ""
        assert text == ""

    def test_whitespace_only(self):
        lang, text = _parse_qwen3_output("   ")
        assert lang == ""
        assert text == ""

    def test_missing_closing_tag(self):
        lang, text = _parse_qwen3_output("language English<asr_text>hello world")
        assert lang == "English"
        assert text == "hello world"

    def test_multiline_metadata(self):
        raw = "language English\nsome extra\n<asr_text>text here</asr_text>"
        lang, text = _parse_qwen3_output(raw)
        assert lang == "English"
        assert text == "text here"

    def test_whitespace_around_text(self):
        lang, text = _parse_qwen3_output(
            "language English<asr_text>  hello  </asr_text>"
        )
        assert lang == "English"
        assert text == "hello"


# ---------------------------------------------------------------------------
# detect_and_fix_repetitions tests
# ---------------------------------------------------------------------------


class TestDetectAndFixRepetitions:
    """Tests for detect_and_fix_repetitions(text, threshold)."""

    def test_normal_text_unchanged(self):
        text = "Hello, how are you?"
        assert detect_and_fix_repetitions(text) == text

    def test_single_char_repeated_above_threshold(self):
        result = detect_and_fix_repetitions("a" * 50)
        assert result == "a"

    def test_single_char_repeated_below_threshold(self):
        text = "a" * 10
        assert detect_and_fix_repetitions(text) == text

    def test_pattern_repeated_above_threshold(self):
        result = detect_and_fix_repetitions("ha" * 30)
        assert result == "ha"

    def test_short_text_unchanged(self):
        assert detect_and_fix_repetitions("hi") == "hi"

    def test_mixed_content_with_repeating_tail(self):
        result = detect_and_fix_repetitions("Hello " + "x" * 50)
        assert result.startswith("Hello ")
        # The long run of x's should be collapsed
        assert len(result) < len("Hello " + "x" * 50)
