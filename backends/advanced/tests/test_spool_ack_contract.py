"""The mobile spool's segment id is not the backend's audio session id.

The phone spools every BLE packet to a file before offering it to the WebSocket, and
that file has an id. It used to travel as ``durable_session_id`` and come back as
``session_id`` — so a spool-file identity wore the name of the backend ``SessionId``,
which is a different thing with a different lifetime (one WebSocket connection, minted
in ``websocket_controller`` as ``{client_id}-{uuid4}``).

Nothing was mis-routed by it, because the receipt key is namespaced by user and client
and the value is only ever echoed back. That is exactly why it is worth pinning: the
names are the only thing keeping the two apart, and the wire is where they meet.

These assert the shape both ends agree on, without a live socket.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "advanced_omi_backend"
APP = Path(__file__).resolve().parents[3] / "app" / "src"

WEBSOCKET_CONTROLLER = SRC / "controllers" / "websocket_controller.py"
AUDIO_STREAMER = APP / "hooks" / "useAudioStreamer.ts"
SPOOL = APP / "services" / "durableAudioSpool.ts"


def _code(path: Path) -> str:
    """File text with comment lines removed, so prose about the old names is ignored."""

    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_backend_reads_and_acknowledges_the_spool_segment_id():
    code = _code(WEBSOCKET_CONTROLLER)

    assert 'chunk_data.get("spool_segment_id")' in code
    assert 'chunk_data.get("spool_sequence")' in code
    # The acknowledgment names what it is acknowledging.
    assert '"spool_segment_id": spool_segment_id' in code


def test_the_backend_never_calls_a_spool_id_a_session_id():
    """``session_id`` in an audio-ack meant the backend audio session, which it isn't."""

    code = _code(WEBSOCKET_CONTROLLER)

    assert "durable_session_id" not in code
    assert "durable_sequence" not in code
    # Find every audio-ack payload and confirm none of them carries a session_id key.
    for block in re.findall(r'"type": "audio-ack",(.{0,240}?)\}', code, re.S):
        assert '"session_id"' not in block, block


@pytest.mark.parametrize("path", [AUDIO_STREAMER, SPOOL])
def test_the_app_uses_segment_naming_end_to_end(path):
    if not path.exists():  # pragma: no cover - app tree absent in some checkouts
        pytest.skip(f"{path} not present")
    code = _code(path)

    assert "durable_session_id" not in code
    assert "sessionId" not in code


def test_the_app_sends_and_matches_the_same_wire_fields_the_backend_reads():
    if not AUDIO_STREAMER.exists():  # pragma: no cover
        pytest.skip("app tree not present")
    app_code = _code(AUDIO_STREAMER)
    backend_code = _code(WEBSOCKET_CONTROLLER)

    # Sent by the app, read by the backend.
    for field in ("spool_segment_id", "spool_sequence"):
        assert field in app_code, field
        assert field in backend_code, field

    # The app retires a pending packet on the key the backend actually sends back.
    assert "msg.spool_segment_id" in app_code
