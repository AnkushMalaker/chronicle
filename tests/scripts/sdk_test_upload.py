#!/usr/bin/env python3
"""Test SDK audio upload."""
import sys
from pathlib import Path

# Add SDK to path
sdk_path = Path(__file__).parent.parent.parent / "sdk" / "python"
sys.path.insert(0, str(sdk_path))

from chronicle_sdk import ChronicleClient

backend_url = sys.argv[1]
email = sys.argv[2]
password = sys.argv[3]
audio_file = sys.argv[4]

client = ChronicleClient(backend_url, timeout=60)
client.login(email, password)
result = client.upload_audio(audio_file)

print(f"STATUS:{result.files[0].status}")
if result.files[0].conversation_id:
    print(f"CONVERSATION_ID:{result.files[0].conversation_id}")
