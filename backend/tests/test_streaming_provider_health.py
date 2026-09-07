"""Transport-health regression tests for registry streaming transcription."""

from types import SimpleNamespace

import pytest

from backend.services.transcription import RegistryStreamingTranscriptionProvider

pytestmark = pytest.mark.unit


class FailingReceiveWebSocket:
    async def send(self, _payload):
        return None

    async def recv(self):
        raise OSError("provider transport closed")


async def test_process_audio_chunk_propagates_transport_failure():
    provider = RegistryStreamingTranscriptionProvider.__new__(
        RegistryStreamingTranscriptionProvider
    )
    provider.model = SimpleNamespace(operations={"expect": {}})
    provider._streams = {
        "session-1": {
            "ws": FailingReceiveWebSocket(),
            "sample_rate": 16000,
            "final": None,
            "interim": [],
            "pending_audio": bytearray(),
        }
    }

    with pytest.raises(OSError, match="provider transport closed"):
        await provider.process_audio_chunk("session-1", bytes(3200))
