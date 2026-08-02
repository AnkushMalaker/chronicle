#!/usr/bin/env python3
"""Record real transcription-provider responses as replayable cassettes.

A cassette is a recorded response from a real provider, keyed by the sha256 of
the audio that produced it. The stub STT replays it, so an assertion about
transcript content holds identically whether the run used a real provider or the
stub -- which is what lets one test suite cover both without any per-test
credential gating.

Provider-neutral by construction: responses are reduced using the same
``response.extract`` paths the model registry already declares, so recording from
a new provider needs no code here.

This is the same "cache a paid API response and reuse it" rule the rest of the
project follows: record once, commit the result, never spend again.

    make record-cassettes PROFILE=smallest-openai

Cassettes are committed. Re-record only when a fixture changes or a provider's
output drifts enough to matter.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
CASSETTE_DIR = TESTS_DIR / "cassettes"
ASSET_DIR = TESTS_DIR / "test_assets"

# Fixtures worth recording: the audio the suite actually asserts against.
FIXTURES = [
    "DIY_Experts_Glass_Blowing_16khz_mono_1min.wav",
    "DIY_Experts_Glass_Blowing_16khz_mono_4min.wav",
]


def pcm_bytes(wav_path: Path) -> tuple[bytes, int]:
    """Return raw little-endian PCM and its sample rate.

    The backend streams headerless PCM to providers (encoding=linear16), so the
    recording must send the same bytes the pipeline would -- otherwise the
    cassette is keyed by audio that never actually reaches a provider.
    """
    with wave.open(str(wav_path), "rb") as wav:
        return wav.readframes(wav.getnframes()), wav.getframerate()


def load_stt_model(config_path: Path) -> dict:
    config = yaml.safe_load(config_path.read_text())
    default_stt = config["defaults"]["stt"]
    for model in config["models"]:
        if model.get("name") == default_stt:
            return model
    sys.exit(f"error: default stt model '{default_stt}' not found in {config_path}")


def expand(value, env: dict) -> str:
    """Resolve OmegaConf-style ${oc.env:VAR,default} used by the test configs."""
    text = str(value)
    if text.startswith("${oc.env:") and text.endswith("}"):
        body = text[len("${oc.env:") : -1]
        name, _, default = body.partition(",")
        return env.get(name.strip(), default.strip())
    return text


def resolve_path(payload, path: str):
    """Follow a dotted extraction path like ``a.b[0].c``.

    Returns None for any missing link rather than raising, so a provider that
    omits an optional field (segments, say) records as empty instead of failing
    the whole recording.
    """
    if not path:
        return None
    current = payload
    for part in path.split("."):
        match = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", part)
        if not match:
            return None
        key, indexes = match.group(1), match.group(2)
        if key:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        for index in re.findall(r"\[(\d+)\]", indexes):
            if not isinstance(current, list) or int(index) >= len(current):
                return None
            current = current[int(index)]
    return current


def normalize(payload: dict, operation: dict) -> dict:
    """Reduce a provider response to the provider-neutral cassette shape.

    Uses the same ``response.extract`` paths the model registry gives the
    backend, so any registry-configured provider can be recorded without
    special-casing it here.
    """
    extract = (operation.get("response") or {}).get("extract") or {}
    return {
        "text": resolve_path(payload, extract.get("text", "")) or "",
        "words": resolve_path(payload, extract.get("words", "")) or [],
        "segments": resolve_path(payload, extract.get("segments", "")) or [],
    }


def record(fixture: str, model: dict, env: dict) -> Path | None:
    wav_path = ASSET_DIR / fixture
    if not wav_path.exists():
        print(f"  skip {fixture}: not found")
        return None

    audio, sample_rate = pcm_bytes(wav_path)
    digest = hashlib.sha256(audio).hexdigest()

    operation = model["operations"]["stt_transcribe"]
    url = expand(model["model_url"], env).rstrip("/") + operation["path"]
    headers = {k: expand(v, env) for k, v in (operation.get("headers") or {}).items()}
    query = {k: expand(v, env) for k, v in (operation.get("query") or {}).items()}
    query.setdefault("sample_rate", str(sample_rate))

    print(
        f"  recording {fixture} ({len(audio) / 1e6:.1f} MB PCM, sha={digest[:12]}) ..."
    )
    response = requests.post(
        url, params=query, headers=headers, data=audio, timeout=300
    )
    if response.status_code != 200:
        sys.exit(
            f"error: provider returned HTTP {response.status_code}: {response.text[:300]}"
        )

    provider = model.get("model_provider", "unknown")
    batch = normalize(response.json(), operation)
    if not batch["text"]:
        sys.exit(
            f"error: recorded an empty transcript from '{provider}'.\n"
            f"       Check the response.extract paths in the profile's config."
        )
    cassette = {
        "fixture": fixture,
        "audio_sha256": digest,
        "sample_rate": sample_rate,
        "recorded_from": {
            "provider": provider,
            "model": query.get("model"),
            "config": str(model.get("name")),
        },
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "batch": batch,
    }

    CASSETTE_DIR.mkdir(exist_ok=True)
    out = CASSETTE_DIR / f"{digest}.json"
    out.write_text(json.dumps(cassette, indent=2, ensure_ascii=False) + "\n")
    words = len(batch["words"])
    print(f"    -> {out.name}: {words} words, {len(batch['segments'])} segments")
    print(f"       text: {batch['text'][:90]}...")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default=os.getenv("PROFILE"),
        help="service profile to record from (default: $PROFILE)",
    )
    parser.add_argument(
        "--config",
        help="test config to record from, bypassing the profile lookup",
    )
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
    else:
        if not args.profile or args.profile == "mock":
            sys.exit(
                "error: recording needs a profile with a real transcription provider.\n"
                "       e.g. make record-cassettes PROFILE=smallest-openai\n"
                "       (see tests/profiles.yml for the available profiles)"
            )
        profiles = yaml.safe_load((TESTS_DIR / "profiles.yml").read_text())["profiles"]
        if args.profile not in profiles:
            sys.exit(f"error: unknown profile '{args.profile}'")
        config_path = TESTS_DIR / profiles[args.profile]["config"]

    model = load_stt_model(config_path)
    env = dict(os.environ)

    api_key = expand(model.get("api_key", ""), env)
    if not api_key:
        sys.exit(
            "error: no API key resolved for the STT provider.\n"
            "       Recording requires real credentials -- this is the one step that does."
        )

    print(f"Recording cassettes from {model.get('description', model.get('name'))}")
    for fixture in FIXTURES:
        record(fixture, model, env)


if __name__ == "__main__":
    main()
