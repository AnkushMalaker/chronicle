#!/usr/bin/env python3
"""Simple enrollment script without global variable issues."""

import sys

import requests

SERVICE_URL = "http://localhost:8085"


def list_speakers():
    """List enrolled speakers."""
    try:
        response = requests.get(f"{SERVICE_URL}/speakers")
        if response.status_code == 200:
            data = response.json()
            speakers = data.get("speakers", {})
            if not speakers:
                print("No speakers enrolled")
            else:
                print(f"Enrolled speakers ({len(speakers)}):")
                for speaker_id, info in speakers.items():
                    print(f"  {speaker_id}: {info.get('name', 'Unknown')}")
            return True
        else:
            print(f"Error: {response.status_code}")
            return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def enroll_speaker(file_path, speaker_id, speaker_name):
    """Enroll a speaker from audio file."""
    try:
        with open(file_path, "rb") as f:
            files = {"file": f}
            data = {"speaker_id": speaker_id, "speaker_name": speaker_name}
            response = requests.post(
                f"{SERVICE_URL}/enroll/upload", files=files, data=data
            )

            if response.status_code == 200:
                result = response.json()
                action = "Updated" if result.get("updated") else "Enrolled"
                print(f"✅ {action} speaker: {speaker_name} (ID: {speaker_id})")
                return True
            else:
                print(f"❌ Error: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        list_speakers()
    elif len(sys.argv) == 4:
        file_path, speaker_id, speaker_name = sys.argv[1], sys.argv[2], sys.argv[3]
        enroll_speaker(file_path, speaker_id, speaker_name)
    else:
        print("Usage:")
        print("  python simple_enroll.py --list")
        print("  python simple_enroll.py <audio_file> <speaker_id> <speaker_name>")
