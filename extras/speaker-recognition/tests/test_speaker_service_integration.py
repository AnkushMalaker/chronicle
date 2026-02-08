#!/usr/bin/env python3
"""
Integration tests for the standalone Speaker Recognition service.

This test suite:
- Starts the speaker service via docker compose (service-only)
- Tests the complete speaker recognition pipeline end-to-end
- Batch enrolls multiple speakers, tests identification, and validates conversation processing

Requirements:
- HF_TOKEN must be set in the environment (pyannote models)
- Docker must be available

Run:
  uv run pytest extras/speaker-recognition/tests/test_speaker_service_integration.py -v -s
"""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

# Test Configuration Flags
# FRESH_RUN=True: Start with fresh data and containers (default)
# CLEANUP_CONTAINERS=True: Stop and remove containers after test (default)
# REBUILD=True: Force rebuild of containers (useful when code changes)
FRESH_RUN = os.environ.get("FRESH_RUN", "true").lower() == "true"
CLEANUP_CONTAINERS = os.environ.get("CLEANUP_CONTAINERS", "true").lower() == "true"
REBUILD = os.environ.get("REBUILD", "false").lower() == "true"

REPO_ROOT = Path(__file__).resolve().parents[3]  # Go up to chronicle root
SPEAKER_DIR = REPO_ROOT / "extras" / "speaker-recognition"
TEST_ASSETS_DIR = SPEAKER_DIR / "tests" / "assets"

# Prefer test compose variant/port; fall back to default if env forces it
COMPOSE_FILE = SPEAKER_DIR / os.environ.get(
    "SPEAKER_COMPOSE_FILE",
    "docker-compose-test.yml",
)

SPEAKER_SERVICE_PORT = int(os.environ.get("SPEAKER_SERVICE_TEST_PORT", os.environ.get("SPEAKER_SERVICE_PORT", "8086")))
SPEAKER_SERVICE_URL = f"http://localhost:{SPEAKER_SERVICE_PORT}"


def _command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _wait_for_health(url: str, timeout_seconds: int = 300) -> None:
    start = time.time()
    last_error = None
    while time.time() - start < timeout_seconds:
        try:
            r = requests.get(f"{url}/health", timeout=5)
            if r.status_code == 200:
                return
            last_error = f"status={r.status_code} body={r.text[:200]}"
        except Exception as e:
            last_error = str(e)
        time.sleep(3)
    raise TimeoutError(f"Speaker service not healthy at {url} within {timeout_seconds}s: {last_error}")


def _compose_up():
    env = os.environ.copy()
    # Ensure port is exported so compose mapping aligns with localhost probing
    # For test compose, we export SPEAKER_SERVICE_TEST_PORT (host mapping)
    if COMPOSE_FILE.name.endswith("docker-compose-test.yml"):
        env.setdefault("SPEAKER_SERVICE_TEST_PORT", str(SPEAKER_SERVICE_PORT))
    else:
        env.setdefault("SPEAKER_SERVICE_PORT", str(SPEAKER_SERVICE_PORT))
    # Pass through HF_TOKEN and optional DEEPGRAM_API_KEY
    if "HF_TOKEN" not in env:
        pytest.skip("HF_TOKEN not set; skipping speaker recognition integration tests")
    if not _command_exists("docker"):
        pytest.skip("Docker not available; skipping speaker recognition integration tests")

    # Determine build flag based on REBUILD setting
    compose_args = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "up",
        "-d",
    ]
    
    if REBUILD:
        compose_args.append("--build")
        print(f"🔨 Rebuilding images from source (REBUILD=True)")
    else:
        print(f"🏗️ Using existing images (REBUILD=False)")
    
    # Service name varies by compose file
    compose_args.append(
        "speaker-service-test" if COMPOSE_FILE.name.endswith("docker-compose-test.yml") else "speaker-service"
    )
    
    try:
        subprocess.run(
            compose_args,
            cwd=str(SPEAKER_DIR),
            env=env,
            check=True,
            # Remove stdout/stderr capture to show Docker build output
        )
    except subprocess.CalledProcessError as e:
        print(f"Docker Compose build failed with exit code {e.returncode}")
        print("Attempting to show Docker logs...")
        try:
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "logs"], cwd=str(SPEAKER_DIR))
        except Exception:
            pass
        raise


def _compose_down():
    if not _command_exists("docker"):
        return
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
            cwd=str(SPEAKER_DIR),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=False)
def speaker_service():
    try:
        if FRESH_RUN:
            print(f"🔄 Fresh run: starting with clean containers and data (FRESH_RUN=True)")
            _compose_down()  # Ensure clean state
        else:
            print(f"♻️ Reuse mode: keeping existing containers and data if available (FRESH_RUN=False)")
        
        _compose_up()
        _wait_for_health(SPEAKER_SERVICE_URL, timeout_seconds=600)
        yield
    finally:
        # Clean up containers based on CLEANUP_CONTAINERS flag
        if CLEANUP_CONTAINERS:
            print(f"🧹 Cleaning up containers (CLEANUP_CONTAINERS=True)")
            _compose_down()
        else:
            print(f"🗂️ Keeping containers running for debugging (CLEANUP_CONTAINERS=False)")


def test_speaker_recognition_pipeline(speaker_service):
    """Test the complete speaker recognition pipeline - enrollment, identification, and conversation analysis."""
    
    print("=" * 80)
    print("🚀 STARTING SPEAKER RECOGNITION INTEGRATION TEST")
    print("=" * 80)
    
    # Phase 1: Service Health Check
    print("📋 Phase 1: Service health check...")
    health_response = requests.get(f"{SPEAKER_SERVICE_URL}/health", timeout=10)
    assert health_response.status_code == 200, f"Health check failed: {health_response.status_code}"
    print("✅ Speaker service is healthy")
    
    # Phase 2: Test Assets Verification
    print("📁 Phase 2: Verifying test assets...")
    evan_dir = TEST_ASSETS_DIR / "evan"
    katelyn_dir = TEST_ASSETS_DIR / "katelyn"
    conversation_file = TEST_ASSETS_DIR / "conversation_evan_katelyn_2min.wav"
    
    assert evan_dir.exists(), f"Evan test assets not found at {evan_dir}"
    assert katelyn_dir.exists(), f"Katelyn test assets not found at {katelyn_dir}"
    assert conversation_file.exists(), f"Conversation file not found at {conversation_file}"
    
    evan_files = list(evan_dir.glob("*.wav"))
    katelyn_files = list(katelyn_dir.glob("*.wav"))
    assert len(evan_files) > 0, "No Evan audio files found"
    assert len(katelyn_files) > 0, "No Katelyn audio files found"
    
    print(f"✅ Found {len(evan_files)} Evan files and {len(katelyn_files)} Katelyn files")
    
    # Phase 3: Speaker Enrollment
    print("👤 Phase 3: Speaker enrollment...")
    
    # 3a. Batch enroll Evan
    print(f"  Enrolling Evan with {len(evan_files)} audio files...")
    files = []
    for i, file_path in enumerate(sorted(evan_files)):
        files.append(("files", (f"evan_{i:03d}.wav", open(file_path, "rb"), "audio/wav")))
    
    data = {"speaker_id": "user_1_evan_test", "speaker_name": "Evan Test"}
    r = requests.post(f"{SPEAKER_SERVICE_URL}/enroll/batch", files=files, data=data, timeout=120)
    
    # Close file handles
    for _, file_tuple in files:
        file_tuple[1].close()
    
    assert r.status_code == 200, f"Evan enrollment failed: {r.status_code} {r.text[:500]}"
    evan_result = r.json()
    assert evan_result.get("speaker_id") == "user_1_evan_test"
    print(f"  ✅ Evan enrolled successfully")
    
    # 3b. Batch enroll Katelyn
    print(f"  Enrolling Katelyn with {len(katelyn_files)} audio files...")
    files = []
    for i, file_path in enumerate(sorted(katelyn_files)):
        files.append(("files", (f"katelyn_{i:03d}.wav", open(file_path, "rb"), "audio/wav")))
    
    data = {"speaker_id": "user_1_katelyn_test", "speaker_name": "Katelyn Test"}
    r = requests.post(f"{SPEAKER_SERVICE_URL}/enroll/batch", files=files, data=data, timeout=120)
    
    # Close file handles
    for _, file_tuple in files:
        file_tuple[1].close()
    
    assert r.status_code == 200, f"Katelyn enrollment failed: {r.status_code} {r.text[:500]}"
    katelyn_result = r.json()
    assert katelyn_result.get("speaker_id") == "user_1_katelyn_test"
    print(f"  ✅ Katelyn enrolled successfully")
    
    # Phase 4: Speaker Database Verification
    print("💾 Phase 4: Speaker database verification...")
    speakers_response = requests.get(f"{SPEAKER_SERVICE_URL}/speakers?user_id=1", timeout=10)
    assert speakers_response.status_code == 200, f"Failed to get speakers: {speakers_response.status_code}"
    speakers_data = speakers_response.json()
    
    assert "speakers" in speakers_data, "No speakers field in response"
    speakers = speakers_data["speakers"]
    assert len(speakers) == 2, f"Expected 2 speakers, got {len(speakers)}"
    
    speaker_ids = [s["id"] for s in speakers]
    assert "user_1_evan_test" in speaker_ids, "Evan not found in speaker list"
    assert "user_1_katelyn_test" in speaker_ids, "Katelyn not found in speaker list"
    print("✅ Both speakers persisted correctly in database")
    
    # Phase 5: Individual Speaker Identification
    print("🔍 Phase 5: Individual speaker identification...")
    
    # 5a. Test Evan identification
    evan_test_file = sorted(evan_files)[0]
    with open(evan_test_file, "rb") as f:
        files = {"file": (evan_test_file.name, f, "audio/wav")}
        data = {"similarity_threshold": "0.10", "user_id": "1"}
        r = requests.post(f"{SPEAKER_SERVICE_URL}/identify", files=files, data=data, timeout=60)
    
    assert r.status_code == 200, f"Evan identify failed: {r.status_code} {r.text[:500]}"
    result = r.json()
    assert result.get("found") is True, "Evan not identified"
    assert result.get("speaker_id") == "user_1_evan_test", f"Wrong speaker identified: {result.get('speaker_id')}"
    evan_confidence = result.get("confidence")
    assert isinstance(evan_confidence, (int, float)), "Invalid confidence value"
    print(f"  ✅ Evan correctly identified with confidence {evan_confidence:.3f}")
    
    # 5b. Test Katelyn identification
    katelyn_test_file = sorted(katelyn_files)[0]
    with open(katelyn_test_file, "rb") as f:
        files = {"file": (katelyn_test_file.name, f, "audio/wav")}
        data = {"similarity_threshold": "0.10", "user_id": "1"}
        r = requests.post(f"{SPEAKER_SERVICE_URL}/identify", files=files, data=data, timeout=60)
    
    assert r.status_code == 200, f"Katelyn identify failed: {r.status_code} {r.text[:500]}"
    result = r.json()
    assert result.get("found") is True, "Katelyn not identified"
    assert result.get("speaker_id") == "user_1_katelyn_test", f"Wrong speaker identified: {result.get('speaker_id')}"
    katelyn_confidence = result.get("confidence")
    assert isinstance(katelyn_confidence, (int, float)), "Invalid confidence value"
    print(f"  ✅ Katelyn correctly identified with confidence {katelyn_confidence:.3f}")
    
    # Phase 6: Conversation Processing (Basic API Functionality)
    print("🗣️ Phase 6: Conversation processing...")
    print("  Note: Testing API functionality, not requiring perfect speaker identification")

    with open(conversation_file, "rb") as f:
        files = {"file": (conversation_file.name, f, "audio/wav")}
        params = {
            "user_id": "1",
            "min_duration": "1.0",
            "similarity_threshold": "0.10",  # Lower threshold for conversation
            "min_speakers": "1",
            "max_speakers": "4",
        }

        print(f"  Processing conversation audio (file size: {conversation_file.stat().st_size / (1024*1024):.1f}MB)...")
        r = requests.post(f"{SPEAKER_SERVICE_URL}/diarize-and-identify", files=files, params=params, timeout=300)

    assert r.status_code == 200, f"Conversation processing failed: {r.status_code} {r.text[:500]}"
    result = r.json()

    # Basic structure validation
    assert "segments" in result, "No segments field in response"
    assert isinstance(result["segments"], list), "Segments is not a list"
    assert len(result["segments"]) > 0, "No segments found in conversation"

    # Count identified vs unknown segments
    identified_segments = 0
    total_segments = len(result["segments"])
    identified_speakers = set()

    for seg in result["segments"]:
        assert "start" in seg and "end" in seg and "speaker" in seg, "Invalid segment structure"

        # Check if speaker was identified (correct field names)
        if seg.get("status") == "identified" and seg.get("identified_id"):
            identified_segments += 1
            speaker_id = seg["identified_id"]  # Direct field access, not nested
            speaker_name = seg.get("identified_as", "")
            confidence = seg.get("confidence", 0.0)
            identified_speakers.add(speaker_id)
            print(f"    Segment identified: {speaker_name} ({speaker_id}) confidence={confidence:.3f}")

    print(f"  ✅ Found {total_segments} segments, {identified_segments} with speaker identification")
    print(f"  ✅ Identified speakers: {identified_speakers}")

    # Success criteria: API works and produces valid output
    # We don't require perfect speaker identification since that depends on audio quality
    assert total_segments > 0, "No segments produced"
    print("✅ Conversation processing API works correctly")

    # Phase 7: Word-Level Data Validation
    print("📝 Phase 7: Validating word-level timestamp data in segments...")
    segments_with_words = 0
    total_words_found = 0

    for seg in result["segments"]:
        # Each segment should have a words array (empty segments might have empty array)
        assert "words" in seg, f"Segment missing 'words' field: {seg}"
        words = seg.get("words", [])

        if len(words) > 0:
            segments_with_words += 1
            total_words_found += len(words)

            # Validate word structure
            for word in words[:3]:  # Check first 3 words of each segment
                assert "word" in word, f"Word missing 'word' field: {word}"
                assert "start" in word, f"Word missing 'start' field: {word}"
                assert "end" in word, f"Word missing 'end' field: {word}"
                # confidence is optional
                assert isinstance(word["start"], (int, float)), f"Word 'start' should be numeric: {word}"
                assert isinstance(word["end"], (int, float)), f"Word 'end' should be numeric: {word}"

    print(f"  ✅ Word-level data: {segments_with_words}/{total_segments} segments have words ({total_words_found} total words)")
    assert segments_with_words > 0, "No segments contain word-level timestamp data"
    assert total_words_found > 0, "No words found across all segments"
    print("✅ Word-level timestamp data validated successfully")

    # Phase 8: Diarize-Identify-Match Endpoint (Backend Integration Mode)
    print("🔗 Phase 8: Testing /v1/diarize-identify-match endpoint (backend integration mode)...")
    print("  This endpoint matches transcript words to diarization segments")

    # Create sample transcript_data with word-level timestamps
    sample_transcript_data = {
        "text": "Hello everyone. This is a test conversation.",
        "words": [
            {"word": "Hello", "start": 0.5, "end": 0.8, "confidence": 0.95},
            {"word": "everyone", "start": 0.9, "end": 1.3, "confidence": 0.92},
            {"word": "This", "start": 2.0, "end": 2.2, "confidence": 0.96},
            {"word": "is", "start": 2.3, "end": 2.4, "confidence": 0.98},
            {"word": "a", "start": 2.5, "end": 2.6, "confidence": 0.97},
            {"word": "test", "start": 2.7, "end": 3.0, "confidence": 0.94},
            {"word": "conversation", "start": 3.1, "end": 3.8, "confidence": 0.93},
        ]
    }

    with open(conversation_file, "rb") as f:
        files = {"file": (conversation_file.name, f, "audio/wav")}
        data = {
            "transcript_data": json.dumps(sample_transcript_data),
            "user_id": "1",
            "min_duration": "1.0",
            "similarity_threshold": "0.10",
            "min_speakers": "1",
            "max_speakers": "4",
        }

        r = requests.post(f"{SPEAKER_SERVICE_URL}/v1/diarize-identify-match", files=files, data=data, timeout=300)

    assert r.status_code == 200, f"/v1/diarize-identify-match failed: {r.status_code} {r.text[:500]}"
    match_result = r.json()

    # Validate response structure
    assert "segments" in match_result, "No segments in diarize-identify-match response"
    match_segments = match_result["segments"]
    assert len(match_segments) > 0, "No segments returned from diarize-identify-match"

    # Validate that segments contain matched words
    match_segments_with_words = 0
    match_total_words = 0

    for seg in match_segments:
        assert "text" in seg, "Segment missing 'text' field"
        assert "words" in seg, "Segment missing 'words' field (should include matched transcript words)"
        words = seg.get("words", [])

        if len(words) > 0:
            match_segments_with_words += 1
            match_total_words += len(words)

            # Validate word structure matches input transcript_data
            for word in words[:2]:  # Check first 2 words
                assert "word" in word, f"Word missing 'word' field: {word}"
                assert "start" in word, f"Word missing 'start' field: {word}"
                assert "end" in word, f"Word missing 'end' field: {word}"

    print(f"  ✅ Diarize-identify-match: {match_segments_with_words}/{len(match_segments)} segments have matched words ({match_total_words} total)")
    assert match_segments_with_words > 0, "No segments contain matched transcript words"
    print("✅ /v1/diarize-identify-match endpoint validated successfully")

    # Final Summary
    print("=" * 80)
    print("🎉 SPEAKER RECOGNITION INTEGRATION TEST COMPLETED SUCCESSFULLY")
    print("=" * 80)
    print(f"✅ Service health: PASS")
    print(f"✅ Evan enrollment: PASS (confidence: {evan_confidence:.3f})")
    print(f"✅ Katelyn enrollment: PASS (confidence: {katelyn_confidence:.3f})")
    print(f"✅ Database persistence: PASS (2 speakers)")
    print(f"✅ Individual identification: PASS (both speakers)")
    print(f"✅ Conversation processing: PASS ({total_segments} segments, {identified_segments} identified)")
    print(f"✅ Word-level timestamps: PASS ({total_words_found} words in {segments_with_words} segments)")
    print(f"✅ Diarize-identify-match: PASS ({match_total_words} matched words in {match_segments_with_words} segments)")
    print("=" * 80)


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v", "-s"])