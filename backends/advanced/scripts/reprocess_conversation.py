#!/usr/bin/env python3
"""Reprocess a conversation's transcript via the backend API.

This triggers POST /api/conversations/{id}/reprocess-transcript, which re-runs
batch transcription using the *currently configured* default batch STT provider
(config/config.yml -> defaults.stt) and then the full post-conversation chain
(speaker recognition -> memory -> title/summary -> dispatch complete).

There is no per-reprocess provider override in the backend, so the "config" is
whatever defaults.stt points at. To reprocess with smallest.ai Pulse (hi), set
  defaults.stt: stt-smallest
in config/config.yml and restart backend + workers, then run this script.

Usage:
    uv run python3 scripts/reprocess_conversation.py <conversation_id> [<conversation_id> ...]

Env (optional, defaults shown):
    BACKEND_URL=http://localhost:8000
    ADMIN_EMAIL / ADMIN_PASSWORD   (else read from backends/advanced/.env)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _read_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'").strip('"')
    return values


def _creds() -> tuple[str, str]:
    env = _read_env_file(ENV_FILE)
    email = os.environ.get("ADMIN_EMAIL") or env.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD")
    if not email or not password:
        sys.exit("ERROR: ADMIN_EMAIL / ADMIN_PASSWORD not found (env or .env).")
    return email, password


def login(email: str, password: str) -> str:
    data = urllib.parse.urlencode({"username": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{BACKEND_URL}/auth/jwt/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read())["access_token"]
    print(f"[auth] logged in as {email}")
    return token


def reprocess(conversation_id: str, token: str) -> None:
    req = urllib.request.Request(
        f"{BACKEND_URL}/api/conversations/{conversation_id}/reprocess-transcript",
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        print(f"[ok] {conversation_id}: {json.dumps(body, indent=2)}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"[FAIL] {conversation_id}: HTTP {e.code} -> {detail}")


def main() -> None:
    ids = sys.argv[1:]
    if not ids:
        sys.exit(__doc__)
    token = login(*_creds())
    for conversation_id in ids:
        reprocess(conversation_id, token)


if __name__ == "__main__":
    main()
