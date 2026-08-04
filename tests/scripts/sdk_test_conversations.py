#!/usr/bin/env python3
"""Test SDK conversation retrieval."""

import sys
from pathlib import Path

# Add SDK to path
sdk_path = Path(__file__).parent.parent.parent / "sdk" / "python"
sys.path.insert(0, str(sdk_path))

from chronicle_sdk import ChronicleClient

backend_url = sys.argv[1]
email = sys.argv[2]
password = sys.argv[3]

client = ChronicleClient(backend_url)
client.login(email, password)
conversations = client.get_conversations(limit=1)
print(f"COUNT:{len(conversations)}")
