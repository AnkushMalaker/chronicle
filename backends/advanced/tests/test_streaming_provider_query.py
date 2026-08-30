import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.services import transcription


@pytest.mark.asyncio
async def test_smallest_stream_preserves_documented_keyword_query(monkeypatch):
    provider = transcription.RegistryStreamingTranscriptionProvider.__new__(
        transcription.RegistryStreamingTranscriptionProvider
    )
    provider.model = SimpleNamespace(
        resolved_url=lambda: "wss://api.smallest.ai/waves/v1/pulse/get_text",
        operations={"query": {"language": "hi", "keywords": "Hermes:1"}},
        model_provider="smallest",
        api_key="secret",
    )
    provider._capabilities = set()
    provider._streams = {}

    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock())
    connect = AsyncMock(return_value=socket)
    monkeypatch.setattr(transcription.websockets, "connect", connect)

    await provider.start_stream("capture-1")

    url = connect.await_args.args[0]
    assert "language=hi" in url
    assert "keywords=Hermes%3A1" in url


@pytest.mark.asyncio
async def test_atomic_audio_v2_frames_are_coalesced_before_provider_poll(monkeypatch):
    provider = transcription.RegistryStreamingTranscriptionProvider.__new__(
        transcription.RegistryStreamingTranscriptionProvider
    )
    provider.model = SimpleNamespace(
        resolved_url=lambda: "wss://example.test/live",
        operations={"query": {}},
        model_provider="test",
        api_key="",
    )
    provider._capabilities = set()
    provider._streams = {}

    socket = SimpleNamespace(
        send=AsyncMock(), recv=AsyncMock(side_effect=asyncio.TimeoutError)
    )
    monkeypatch.setattr(
        transcription.websockets, "connect", AsyncMock(return_value=socket)
    )
    await provider.start_stream("capture-1")

    for _ in range(4):
        assert await provider.process_audio_chunk("capture-1", bytes(640)) is None
    socket.send.assert_not_awaited()

    assert await provider.process_audio_chunk("capture-1", bytes(640)) is None
    socket.send.assert_awaited_once_with(bytes(3200))
