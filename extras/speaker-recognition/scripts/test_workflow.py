#!/usr/bin/env python3
"""
Test script to demonstrate the complete speaker enrollment and identification workflow.

This script shows how to:
1. Enroll speakers with sample audio
2. Run diarization and identification on a conversation
3. Display results

Usage:
    python test_workflow.py
"""

import os
import sys
from pathlib import Path

import requests

SPEAKER_SERVICE_URL = os.getenv("SPEAKER_SERVICE_URL", "http://localhost:8085")


def test_enrollment():
    """Test speaker enrollment with sample files."""
    print("\n=== Testing Speaker Enrollment ===")

    # Check if we have sample audio files
    sample_dir = Path(__file__).parent / "training/youtube-transcript/data/audio"
    if not sample_dir.exists():
        print(f"❌ Sample audio directory not found: {sample_dir}")
        print(
            "Please ensure you have audio files in the training/youtube-transcript/data/audio/ directory"
        )
        return False

    audio_files = list(sample_dir.glob("*.wav"))
    if not audio_files:
        print(f"❌ No WAV files found in {sample_dir}")
        return False

    print(f"Found {len(audio_files)} audio files")

    # Enroll first speaker using first audio file
    if len(audio_files) >= 1:
        file1 = audio_files[0]
        print(f"\nEnrolling Speaker 1 from: {file1.name}")

        with open(file1, "rb") as f:
            files = {"file": (file1.name, f, "audio/wav")}
            data = {"speaker_id": "test_speaker_1", "speaker_name": "Test Speaker 1"}

            response = requests.post(
                f"{SPEAKER_SERVICE_URL}/enroll/upload", files=files, data=data
            )
            if response.status_code == 200:
                print("✅ Successfully enrolled Test Speaker 1")
            else:
                print(f"❌ Failed to enroll: {response.status_code} - {response.text}")
                return False

    # Enroll second speaker if we have another file
    if len(audio_files) >= 2:
        file2 = audio_files[1]
        print(f"\nEnrolling Speaker 2 from: {file2.name}")

        with open(file2, "rb") as f:
            files = {"file": (file2.name, f, "audio/wav")}
            data = {"speaker_id": "test_speaker_2", "speaker_name": "Test Speaker 2"}

            response = requests.post(
                f"{SPEAKER_SERVICE_URL}/enroll/upload", files=files, data=data
            )
            if response.status_code == 200:
                print("✅ Successfully enrolled Test Speaker 2")
            else:
                print(f"❌ Failed to enroll: {response.status_code} - {response.text}")

    return True


def test_diarize_and_identify():
    """Test the diarize-and-identify endpoint."""
    print("\n=== Testing Diarize and Identify ===")

    # Use one of the sample files for testing
    sample_dir = Path(__file__).parent / "training/youtube-transcript/data/audio"
    audio_files = list(sample_dir.glob("*.wav"))

    if not audio_files:
        print("❌ No audio files available for testing")
        return False

    # Use the first file for diarization test
    test_file = audio_files[0]
    print(f"Testing with: {test_file.name}")

    with open(test_file, "rb") as f:
        files = {"file": (test_file.name, f, "audio/wav")}
        data = {
            "min_duration": 0.5,
            "similarity_threshold": 0.65,
            "identify_only_enrolled": False,
        }

        print("Sending request to /diarize-and-identify...")
        response = requests.post(
            f"{SPEAKER_SERVICE_URL}/diarize-and-identify", files=files, data=data
        )

        if response.status_code == 200:
            result = response.json()
            print("\n✅ Diarization and identification successful!")

            # Display summary
            summary = result.get("summary", {})
            print(f"\nSummary:")
            print(f"  Total Duration: {summary.get('total_duration', 0):.2f}s")
            print(f"  Segments: {summary.get('num_segments', 0)}")
            print(f"  Diarized Speakers: {summary.get('num_diarized_speakers', 0)}")
            print(f"  Identified Speakers: {summary.get('identified_speakers', [])}")
            print(f"  Unknown Speakers: {summary.get('unknown_speakers', [])}")
            print(f"  Similarity Threshold: {summary.get('similarity_threshold', 0)}")

            # Display first few segments
            segments = result.get("segments", [])
            if segments:
                print(f"\nFirst {min(5, len(segments))} segments:")
                for i, seg in enumerate(segments[:5]):
                    print(f"\n  Segment {i+1}:")
                    print(f"    Time: {seg['start']:.2f}s - {seg['end']:.2f}s")
                    print(f"    Speaker: {seg['speaker']}")
                    print(f"    Identified As: {seg.get('identified_as', 'Unknown')}")
                    print(f"    Confidence: {seg.get('confidence', 0):.3f}")
                    print(f"    Status: {seg.get('status', 'unknown')}")

            return True
        else:
            print(f"❌ Request failed: {response.status_code} - {response.text}")
            return False


def list_speakers():
    """List all enrolled speakers."""
    print("\n=== Enrolled Speakers ===")

    response = requests.get(f"{SPEAKER_SERVICE_URL}/speakers")
    if response.status_code == 200:
        data = response.json()
        speakers = data.get("speakers", {})

        if not speakers:
            print("No speakers enrolled")
        else:
            for speaker_id, info in speakers.items():
                print(f"\nID: {speaker_id}")
                print(f"  Name: {info.get('name', 'Unknown')}")
                print(f"  FAISS Index: {info.get('faiss_index', 'N/A')}")

        return True
    else:
        print(f"❌ Failed to list speakers: {response.status_code}")
        return False


def cleanup():
    """Clean up test speakers."""
    print("\n=== Cleaning Up Test Data ===")

    # Delete test speakers
    for speaker_id in ["test_speaker_1", "test_speaker_2"]:
        response = requests.delete(f"{SPEAKER_SERVICE_URL}/speakers/{speaker_id}")
        if response.status_code == 200:
            print(f"✅ Deleted {speaker_id}")
        elif response.status_code == 404:
            print(f"ℹ️  {speaker_id} not found (already deleted)")


def main():
    print("Speaker Recognition Workflow Test")
    print("=" * 50)

    # Check service health
    try:
        response = requests.get(f"{SPEAKER_SERVICE_URL}/health", timeout=5)
        if response.status_code != 200:
            print(f"❌ Speaker service not responding at {SPEAKER_SERVICE_URL}")
            return 1
        health = response.json()
        print(f"✅ Service running (Device: {health.get('device', 'unknown')})")
    except Exception as e:
        print(f"❌ Cannot connect to speaker service: {e}")
        print("Make sure the service is running: docker compose up speaker-recognition")
        return 1

    # Run tests
    try:
        # Test enrollment
        if not test_enrollment():
            print("\n❌ Enrollment test failed")
            return 1

        # List speakers
        list_speakers()

        # Test diarize and identify
        if not test_diarize_and_identify():
            print("\n❌ Diarize and identify test failed")
            return 1

        print("\n✅ All tests passed!")

    finally:
        # Always cleanup
        cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
