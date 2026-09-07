import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from backend.services import transcription

ROOT = Path(__file__).resolve().parents[2]


def test_vibevoice_streaming_config_declares_generic_context_field():
    config = yaml.safe_load((ROOT / "config" / "defaults.yml").read_text())
    model = next(
        entry
        for entry in config["models"]
        if entry["name"] == "stt-vibevoice-streaming-1.5b"
    )

    assert model["model_type"] == "stt_stream"
    assert model["operations"]["start"]["context_field"] == "context_info"
    assert "context_prompt" in model["capabilities"]
    assert "keyword_boosting" not in model["capabilities"]


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
async def test_registry_start_message_excludes_hot_words_from_context_prompt(
    monkeypatch,
):
    provider = transcription.RegistryStreamingTranscriptionProvider.__new__(
        transcription.RegistryStreamingTranscriptionProvider
    )
    provider.model = SimpleNamespace(
        resolved_url=lambda: "ws://vibevoice.test/stream",
        operations={
            "query": {},
            "start": {
                "message": {"type": "start", "sample_rate": 16000},
                "context_field": "context_info",
            },
        },
        name="stt-vibevoice-streaming-1.5b",
        model_provider="vibevoice-streaming",
        api_key="",
    )
    provider._capabilities = {transcription.CAP_CONTEXT_PROMPT}
    provider._streams = {}

    prompt_registry = SimpleNamespace(
        get_prompt=AsyncMock(return_value="Hermes, Chronicle")
    )
    monkeypatch.setattr(transcription, "get_prompt_registry", lambda: prompt_registry)
    monkeypatch.setattr(
        transcription, "_get_plugin_keywords", lambda: ["Wake phrase", "Friend"]
    )
    monkeypatch.setattr(
        transcription,
        "_resolve_asr_context",
        lambda _model: "Product names may appear in this recording.",
    )
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(return_value="ready"))
    monkeypatch.setattr(
        transcription.websockets, "connect", AsyncMock(return_value=socket)
    )

    await provider.start_stream("capture-1")

    start_message = json.loads(socket.send.await_args.args[0])
    assert start_message["type"] == "start"
    assert start_message["session_id"] == "capture-1"
    assert (
        start_message["context_info"] == "Product names may appear in this recording."
    )
    assert "Hermes" not in start_message["context_info"]
    assert "Wake phrase" not in start_message["context_info"]


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
