#!/usr/bin/env python3
"""Test the diarize-and-identify endpoint."""

import sys

import requests

SERVICE_URL = "http://localhost:8085"


def test_diarize_and_identify(audio_file):
    """Test diarization and identification."""
    try:
        with open(audio_file, "rb") as f:
            files = {"file": f}
            data = {
                "min_duration": 0.5,
                "similarity_threshold": 0.65,
                "identify_only_enrolled": False,
            }

            print(f"Testing diarization and identification on: {audio_file}")
            response = requests.post(
                f"{SERVICE_URL}/diarize-and-identify", files=files, data=data
            )

            if response.status_code == 200:
                result = response.json()
                print("\n✅ Success!")

                # Show summary
                summary = result.get("summary", {})
                print(f"\nSummary:")
                print(f"  Duration: {summary.get('total_duration', 0):.2f}s")
                print(f"  Segments: {summary.get('num_segments', 0)}")
                print(f"  Diarized Speakers: {summary.get('num_diarized_speakers', 0)}")
                print(f"  Identified: {summary.get('identified_speakers', [])}")
                print(f"  Unknown: {summary.get('unknown_speakers', [])}")

                # Show segments
                segments = result.get("segments", [])
                print(f"\nSegments:")
                for i, seg in enumerate(segments):
                    identified = seg.get("identified_as", "Unknown")
                    confidence = seg.get("confidence", 0)
                    print(
                        f"  {i+1}. {seg['start']:.2f}s-{seg['end']:.2f}s: {seg['speaker']} → {identified} ({confidence:.3f})"
                    )

                return True
            else:
                print(f"❌ Error: {response.status_code} - {response.text}")
                return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python test_diarize.py <audio_file>")
        sys.exit(1)

    test_diarize_and_identify(sys.argv[1])
