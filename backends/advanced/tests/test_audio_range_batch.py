import io
import wave

import numpy as np
import pytest

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.utils import audio_chunk_utils


class ConversationCollection:
    def __init__(self):
        self.calls = []

    async def find_one(self, query, projection):
        self.calls.append((query, projection))
        return {"audio_total_duration": 10.0}


class ChunkCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *_args):
        return self

    async def to_list(self, length=None):
        return self.documents


class ChunkCollection:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    def find(self, query, projection):
        self.calls.append((query, projection))
        return ChunkCursor(self.documents)


def first_pcm_sample(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        return np.frombuffer(wav_file.readframes(1), dtype="<i2")[0]


@pytest.mark.asyncio
async def test_reconstruct_audio_ranges_loads_metadata_and_decodes_window_once(
    monkeypatch,
):
    conversation_collection = ConversationCollection()
    chunk_collection = ChunkCollection(
        [
            {
                "audio_data": b"opus",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
                "sample_rate": 16_000,
                "channels": 1,
            }
        ]
    )
    pcm = np.repeat(np.arange(10, dtype="<i2") * 100, 16_000).tobytes()
    decode_calls = []

    async def decode(opus_data, sample_rate, channels):
        decode_calls.append((opus_data, sample_rate, channels))
        return pcm

    monkeypatch.setattr(
        Conversation,
        "get_pymongo_collection",
        lambda: conversation_collection,
    )
    monkeypatch.setattr(
        AudioChunkDocument,
        "get_pymongo_collection",
        lambda: chunk_collection,
    )
    monkeypatch.setattr(audio_chunk_utils, "decode_opus_to_pcm", decode)

    results = await audio_chunk_utils.reconstruct_audio_ranges(
        "conversation-1",
        [(3.0, 4.0), (1.0, 2.0)],
    )
    scalar = await audio_chunk_utils.reconstruct_audio_segment(
        "conversation-1",
        2.0,
        3.0,
    )

    assert [first_pcm_sample(result) for result in results] == [300, 100]
    assert first_pcm_sample(scalar) == 200
    assert conversation_collection.calls == [
        (
            {"conversation_id": "conversation-1", "deleted": {"$ne": True}},
            {"_id": 0, "audio_total_duration": 1},
        ),
        (
            {"conversation_id": "conversation-1", "deleted": {"$ne": True}},
            {"_id": 0, "audio_total_duration": 1},
        ),
    ]
    assert len(chunk_collection.calls) == 2
    assert decode_calls == [(b"opus", 16_000, 1), (b"opus", 16_000, 1)]


@pytest.mark.asyncio
async def test_reconstruct_audio_ranges_decodes_across_unrequested_gap_as_islands(
    monkeypatch,
):
    conversation_collection = ConversationCollection()
    chunk_collection = ChunkCollection(
        [
            {
                "audio_data": b"first",
                "start_time": 0.0,
                "end_time": 4.0,
                "chunk_index": 0,
                "sample_rate": 16_000,
                "channels": 1,
            },
            {
                "audio_data": b"second",
                "start_time": 6.0,
                "end_time": 10.0,
                "chunk_index": 1,
                "sample_rate": 16_000,
                "channels": 1,
            },
        ]
    )
    decode_calls = []

    async def decode(opus_data, sample_rate, channels):
        decode_calls.append(opus_data)
        value = 100 if opus_data == b"first" else 200
        return np.full(4 * sample_rate, value, dtype="<i2").tobytes()

    monkeypatch.setattr(
        Conversation,
        "get_pymongo_collection",
        lambda: conversation_collection,
    )
    monkeypatch.setattr(
        AudioChunkDocument,
        "get_pymongo_collection",
        lambda: chunk_collection,
    )
    monkeypatch.setattr(audio_chunk_utils, "decode_opus_to_pcm", decode)

    results = await audio_chunk_utils.reconstruct_audio_ranges(
        "conversation-1",
        [(1.0, 2.0), (7.0, 8.0)],
    )

    assert [first_pcm_sample(result) for result in results] == [100, 200]
    assert decode_calls == [b"first", b"second"]
