"""The mobile spool's segment id is not the backend's audio session id.

The phone spools every BLE packet to a file before offering it to the WebSocket, and
that file has an id. It used to travel as ``durable_session_id`` and come back as
``session_id`` — so a spool-file identity wore the name of the backend ``SessionId``,
which is a different thing with a different lifetime (one WebSocket connection, minted
for each authenticated audio-v2 connection as ``{client_id}-{uuid4}``).

Nothing was mis-routed by it, because the receipt key is namespaced by user and client
and the value is only ever echoed back. That is exactly why it is worth pinning: the
names are the only thing keeping the two apart, and the wire is where they meet.

These assert the shape both ends agree on, without a live socket.
"""

from pathlib import Path

import pytest
from google.protobuf.descriptor import FieldDescriptor

from backend.audio_contract.v2 import audio_pb2

SRC = Path(__file__).resolve().parents[1] / "src" / "backend"
APP = Path(__file__).resolve().parents[2] / "app" / "src"

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


def test_v2_ack_is_bound_to_the_backend_capture_and_exact_packet_sequence():
    """The V2 receipt cannot confuse a local spool file with a capture session."""

    fields = audio_pb2.CapturePacketAccepted.DESCRIPTOR.fields_by_name
    assert fields["binding"].message_type.full_name.endswith("CaptureBinding")
    assert fields["sequence"].type == FieldDescriptor.TYPE_UINT64
    assert "spool_segment_id" not in fields
    assert "session_id" not in fields


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
    # Spool identity stays local. The client maps a generated packet sequence back
    # to its pending file and retires it only on CapturePacketAccepted.
    assert "acceptedRef" in app_code
    assert "onPacketAccepted" in app_code
    assert "durableAudioSpool.acknowledge(packet)" in app_code
    assert "spool_segment_id" not in app_code
