"""Utility functions for memory extraction and parsing."""

import json
import logging
import re
from typing import Any, Dict, Optional

memory_logger = logging.getLogger("memory_service")


def extract_json_from_text(response_text: str) -> Optional[Dict[str, Any]]:
    """
    Extract JSON from LLM response, handling various formats including reasoning tokens.

    This function handles:
    - Clean JSON responses
    - Responses with <think> tags before JSON
    - Responses with extra text around JSON
    - Multiple JSON objects (returns the first valid one)
    - Memory update format with "memory" key
    """
    if not response_text or not response_text.strip():
        memory_logger.warning("Empty response received from LLM")
        return None

    # First, try to parse the response as-is (for clean JSON responses)
    try:
        parsed = json.loads(response_text.strip())
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Handle <think> tags - extract content after </think>
    if "<think>" in response_text and "</think>" in response_text:
        try:
            # Find the end of the thinking section
            think_end = response_text.find("</think>")
            if think_end != -1:
                # Extract everything after </think>
                json_part = response_text[think_end + 8 :].strip()

                if json_part:
                    try:
                        parsed = json.loads(json_part)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        memory_logger.debug(
                            f"Failed to parse post-think JSON: {json_part[:100]}..."
                        )
                        # Continue to other strategies
        except Exception as e:
            memory_logger.debug(f"Error handling think tags: {e}")

    # Clean up common LLM response artifacts
    cleaned_text = response_text
    # Remove markdown code blocks
    cleaned_text = re.sub(
        r"```(?:json)?\s*(.*?)\s*```", r"\1", cleaned_text, flags=re.DOTALL
    )
    # Remove common prefixes
    cleaned_text = re.sub(r"^.*?(?=\{)", "", cleaned_text, flags=re.DOTALL)
    # Remove trailing non-JSON content
    cleaned_text = re.sub(r"\}.*$", "}", cleaned_text, flags=re.DOTALL)

    # Try parsing the cleaned text
    try:
        parsed = json.loads(cleaned_text.strip())
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try to find JSON using comprehensive regex patterns
    json_patterns = [
        # Look for memory format: {"memory": [...]}
        r'\{"memory"\\s*:\\s*\[.*?\]\\s*\}',
        # Look for facts format: {"facts": [...]}
        r'\{"facts"\\s*:\\s*\[.*?\]\\s*\}',
        # Look for any JSON object containing memory or facts
        r'\{[^{}]*"(?:memory|facts)"[^{}]*\}',
        # Look for any balanced JSON object
        r"\{(?:[^{}]|{[^{}]*})*\}",
    ]

    for pattern in json_patterns:
        try:
            matches = re.findall(pattern, response_text, re.DOTALL)
            for match in matches:
                try:
                    parsed = json.loads(match)
                    if isinstance(parsed, dict):
                        # Prefer responses with expected keys
                        if "memory" in parsed or "facts" in parsed:
                            memory_logger.debug(
                                f"Successfully extracted JSON using pattern: {pattern}"
                            )
                            return parsed
                        # Keep as fallback
                        fallback = parsed
                except json.JSONDecodeError:
                    continue
            # Use fallback if we found a valid dict but without preferred keys
            if "fallback" in locals():
                return fallback
        except Exception as e:
            memory_logger.debug(f"Pattern {pattern} failed: {e}")
            continue

    # Try to extract just the facts or memory array if JSON object parsing fails
    for key in ["memory", "facts"]:
        array_pattern = f'"{key}"\\s*:\\s*(\\[.*?\\])'
        try:
            match = re.search(array_pattern, response_text, re.DOTALL)
            if match:
                array_str = match.group(1)
                array_data = json.loads(array_str)
                if isinstance(array_data, list):
                    memory_logger.debug(
                        f"Successfully extracted {key} array from response"
                    )
                    return {key: array_data}
        except Exception as e:
            memory_logger.debug(f"{key} array extraction failed: {e}")

    # Last resort: try to find any JSON-like structure
    try:
        # Look for anything that starts with { and ends with }
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}")

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            potential_json = response_text[start_idx : end_idx + 1]
            try:
                parsed = json.loads(potential_json)
                if isinstance(parsed, dict):
                    memory_logger.debug(
                        "Successfully extracted JSON using bracket matching"
                    )
                    return parsed
            except json.JSONDecodeError:
                pass
    except Exception as e:
        memory_logger.debug(f"Bracket matching failed: {e}")

    # If all else fails, log the problematic response for debugging
    memory_logger.error(
        f"Failed to extract JSON from LLM response. Response preview: {response_text[:200]}..."
    )
    return None
