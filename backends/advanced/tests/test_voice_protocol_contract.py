import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from advanced_omi_backend.voice_protocol import parse_voice_protocol_event

CONTRACT_ROOT = (
    Path(__file__).resolve().parents[3] / "contracts" / "voice_protocol" / "v1"
)


@pytest.mark.parametrize("fixture", sorted((CONTRACT_ROOT / "golden").glob("*.json")))
def test_protocol_v1_golden_fixture_is_strictly_valid(fixture: Path):
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    parsed = parse_voice_protocol_event(payload)

    assert parsed.type == payload["type"]
    assert parsed.model_dump(mode="json", exclude_none=False) == payload


@pytest.mark.parametrize("fixture", sorted((CONTRACT_ROOT / "invalid").glob("*.json")))
def test_protocol_v1_invalid_fixture_is_rejected(fixture: Path):
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        parse_voice_protocol_event(payload)


def test_protocol_event_never_accepts_user_identity_from_the_wire():
    payload = json.loads(
        (CONTRACT_ROOT / "golden" / "voice-session.ready.json").read_text(
            encoding="utf-8"
        )
    )
    payload["user_id"] = "attacker-selected-user"

    with pytest.raises(ValidationError):
        parse_voice_protocol_event(payload)
