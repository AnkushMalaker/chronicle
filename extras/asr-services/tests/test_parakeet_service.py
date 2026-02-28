#!/usr/bin/env python3
"""
Pure ASR Service Test for Parakeet HTTP API

Tests only the Parakeet ASR service directly via HTTP API to verify:
1. Service responds to health checks
2. Batch transcription endpoint works
3. Word-level timestamps are returned
4. Basic transcription quality

This test is self-contained and manages its own Docker service lifecycle.
No external service management required.

Run with:
  # Run the test (service management is automatic)
  cd /home/ankush/workspaces/chronicle/extras/asr-services
  uv run pytest tests/test_parakeet_service.py -v -s
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import pytest
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Test Configuration
PARAKEET_SERVICE_URL = os.getenv("PARAKEET_SERVICE_URL", "http://localhost:8767")
PARAKEET_TEST_COMPOSE_FILE = os.getenv(
    "PARAKEET_TEST_COMPOSE_FILE", "docker-compose-test.yml"
)
PARAKEET_TEST_SERVICE = os.getenv("PARAKEET_TEST_SERVICE", "parakeet-asr-test")
HEALTH_CHECK_TIMEOUT = int(os.getenv("PARAKEET_HEALTH_TIMEOUT", "180"))
REQUEST_TIMEOUT = int(os.getenv("PARAKEET_REQUEST_TIMEOUT", "240"))

# Test files
tests_dir = Path(__file__).parent
TEST_AUDIO_PATH = tests_dir / "assets" / "test_clip_10s.wav"
if not TEST_AUDIO_PATH.exists():
    repo_root = tests_dir.parent.parent.parent
    TEST_AUDIO_PATH = (
        repo_root
        / "tests"
        / "test_assets"
        / "DIY_Experts_Glass_Blowing_16khz_mono_1min.wav"
    )


class ParakeetServiceTester:
    """Manages Parakeet ASR service testing with Docker lifecycle management."""

    def __init__(self, service_url: str = PARAKEET_SERVICE_URL):
        self.service_url = service_url.rstrip("/")
        self.health_url = f"{self.service_url}/health"
        self.transcribe_url = f"{self.service_url}/transcribe"
        self.service_started = False
        self.asr_dir = Path(__file__).parent.parent  # extras/asr-services directory

    def start_service(self):
        """Start Parakeet ASR service using Docker Compose."""
        logger.info("🚀 Starting Parakeet ASR service...")

        try:
            # Stop any existing services first
            subprocess.run(
                ["docker", "compose", "-f", PARAKEET_TEST_COMPOSE_FILE, "down"],
                cwd=self.asr_dir,
                capture_output=True,
            )

            # Start Parakeet ASR service
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    PARAKEET_TEST_COMPOSE_FILE,
                    "up",
                    "--build",
                    "-d",
                    PARAKEET_TEST_SERVICE,
                ],
                cwd=self.asr_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.error(f"Failed to start Parakeet service: {result.stderr}")
                raise RuntimeError(f"Parakeet service failed to start: {result.stderr}")

            self.service_started = True
            logger.info("✅ Parakeet ASR service started successfully")

        except Exception as e:
            logger.error(f"Error starting Parakeet service: {e}")
            raise

    def cleanup_service(self):
        """Clean up Parakeet ASR service."""
        if not self.service_started:
            return

        logger.info("🧹 Cleaning up Parakeet ASR service...")

        try:
            result = subprocess.run(
                ["docker", "compose", "-f", PARAKEET_TEST_COMPOSE_FILE, "down"],
                cwd=self.asr_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.warning(f"Service cleanup may have failed: {result.stderr}")
            else:
                logger.info("✅ Parakeet ASR service cleaned up")

        except Exception as e:
            logger.warning(f"Error during service cleanup: {e}")
        finally:
            self.service_started = False

    def wait_for_service_ready(self, timeout: int = HEALTH_CHECK_TIMEOUT) -> bool:
        """Wait for Parakeet service to be ready."""
        logger.info(f"🔍 Waiting for Parakeet service at {self.service_url}...")

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(self.health_url, timeout=5)
                if response.status_code == 200:
                    health_data = response.json()
                    logger.info(f"✅ Parakeet service is ready: {health_data}")
                    return True
                else:
                    logger.debug(
                        f"Health check failed with status {response.status_code}"
                    )
            except requests.exceptions.RequestException as e:
                logger.debug(f"Health check request failed: {e}")

            time.sleep(2)

        logger.error(f"❌ Parakeet service failed to become ready within {timeout}s")
        return False

    def transcribe_audio_file(self, audio_path: Path) -> Dict[str, Any]:
        """Transcribe audio file using the batch API."""
        logger.info(f"📤 Transcribing audio file: {audio_path.name}")

        if not audio_path.exists():
            raise FileNotFoundError(f"Test audio file not found: {audio_path}")

        try:
            with open(audio_path, "rb") as audio_file:
                files = {"file": (audio_path.name, audio_file, "audio/wav")}

                response = requests.post(
                    self.transcribe_url, files=files, timeout=REQUEST_TIMEOUT
                )

            if response.status_code != 200:
                raise RuntimeError(
                    f"Transcription request failed: {response.status_code} - {response.text}"
                )

            result = response.json()
            logger.info(
                f"✅ Transcription completed: {len(result.get('text', ''))} chars"
            )
            return result

        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            raise


@pytest.fixture
def parakeet_tester():
    """Pytest fixture for Parakeet service tester with automatic lifecycle management."""
    tester = ParakeetServiceTester()

    try:
        # Start the service
        tester.start_service()

        # Wait for it to be ready
        if not tester.wait_for_service_ready():
            raise RuntimeError("Parakeet service failed to become ready")

        yield tester

    finally:
        # Always clean up, even if test fails
        tester.cleanup_service()


@pytest.mark.asr
def test_parakeet_service_health(parakeet_tester):
    """Test that Parakeet service health endpoint works."""
    logger.info("🏥 Testing Parakeet service health check...")

    # Service should already be ready from fixture
    response = requests.get(parakeet_tester.health_url, timeout=5)
    assert response.status_code == 200, f"Health check failed: {response.status_code}"

    health_data = response.json()
    assert "status" in health_data, "Health response missing status field"
    assert health_data["status"] == "healthy", f"Service not healthy: {health_data}"

    logger.info("✅ Health check passed")


@pytest.mark.asr
def test_parakeet_batch_transcription(parakeet_tester):
    """Test batch transcription with word-level timestamps."""
    logger.info("🎯 Testing Parakeet batch transcription with word-level timestamps...")

    # Check test audio file exists
    assert TEST_AUDIO_PATH.exists(), f"Test audio file missing: {TEST_AUDIO_PATH}"

    # Perform transcription
    result = parakeet_tester.transcribe_audio_file(TEST_AUDIO_PATH)

    # Validate response structure
    assert isinstance(result, dict), "Response should be a JSON object"
    assert "text" in result, "Response missing 'text' field"
    assert "words" in result, "Response missing 'words' field"
    assert "segments" in result, "Response missing 'segments' field"

    # Validate transcription content
    text = result["text"]
    words = result["words"]

    assert isinstance(text, str), "Text should be a string"
    assert isinstance(words, list), "Words should be a list"
    assert len(text.strip()) > 0, "Transcription text should not be empty"

    logger.info(f"📝 Transcribed text: {text}")
    logger.info(f"🔢 Word count: {len(words)}")

    # Validate word-level timestamps
    if words:  # Only check if we have words
        for i, word_data in enumerate(words[:5]):  # Check first 5 words
            logger.info(f"Word {i + 1}: {word_data}")

            # Check required fields
            assert "word" in word_data, f"Word {i + 1} missing 'word' field"
            assert "start" in word_data, f"Word {i + 1} missing 'start' field"
            assert "end" in word_data, f"Word {i + 1} missing 'end' field"
            assert "confidence" in word_data, f"Word {i + 1} missing 'confidence' field"

            # Check data types
            assert isinstance(
                word_data["word"], str
            ), f"Word {i + 1} 'word' should be string"
            assert isinstance(
                word_data["start"], (int, float)
            ), f"Word {i + 1} 'start' should be number"
            assert isinstance(
                word_data["end"], (int, float)
            ), f"Word {i + 1} 'end' should be number"
            assert isinstance(
                word_data["confidence"], (int, float)
            ), f"Word {i + 1} 'confidence' should be number"

            # Check timestamp logic
            assert word_data["start"] >= 0, f"Word {i + 1} start time should be >= 0"
            assert (
                word_data["end"] >= word_data["start"]
            ), f"Word {i + 1} end should be >= start"
            assert (
                0 <= word_data["confidence"] <= 1
            ), f"Word {i + 1} confidence should be 0-1"

        logger.info("✅ Word-level timestamps validated successfully")
    else:
        logger.warning(
            "⚠️ No words returned - this may indicate an issue with the audio or model"
        )

    # Basic quality check - transcript should contain some meaningful content
    assert len(text.split()) >= 3, "Transcript should contain at least 3 words"

    logger.info("✅ Batch transcription test passed")


@pytest.mark.asr
def test_parakeet_transcription_quality(parakeet_tester):
    """Test transcription quality and expected content."""
    logger.info("🎯 Testing transcription quality...")

    # Perform transcription
    result = parakeet_tester.transcribe_audio_file(TEST_AUDIO_PATH)

    text = result["text"].lower()
    words = result["words"]

    # Basic quality metrics
    word_count = len(text.split())
    char_count = len(text.strip())

    logger.info(f"📊 Quality metrics:")
    logger.info(f"  - Characters: {char_count}")
    logger.info(f"  - Words: {word_count}")
    logger.info(f"  - Word objects: {len(words)}")

    # Quality assertions
    assert char_count >= 20, "Transcript too short - may indicate poor quality"
    assert word_count >= 5, "Too few words detected"

    # If we got word-level data, word count should roughly match
    if words:
        word_object_count = len(words)
        # Allow some variance due to punctuation/formatting differences
        ratio = word_object_count / word_count if word_count > 0 else 0
        assert (
            0.5 <= ratio <= 2.0
        ), f"Word count mismatch: text={word_count}, objects={word_object_count}"

    logger.info("✅ Transcription quality check passed")


if __name__ == "__main__":
    # Allow running directly for debugging
    import sys

    print("🚀 Running Parakeet ASR service tests...")
    print("Service will be started automatically - no manual setup required!")
    print()

    tester = ParakeetServiceTester()

    try:
        # Start service and wait for readiness
        tester.start_service()
        if not tester.wait_for_service_ready():
            raise RuntimeError("Service failed to become ready")

        # Run tests manually
        test_parakeet_service_health(tester)
        test_parakeet_batch_transcription(tester)
        test_parakeet_transcription_quality(tester)

        print("🎉 All tests passed!")

    except Exception as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)
    finally:
        tester.cleanup_service()
