import io
import threading
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from advanced_omi_backend.services import audio_claims
from advanced_omi_backend.services.audio_claims import ClaimedChunk
from advanced_omi_backend.utils import audio_chunk_utils


def _claimed(
    chunk_id: str,
    opus: bytes,
    *,
    conversation_start: float,
    duration: float,
) -> ClaimedChunk:
    chunk = SimpleNamespace(
        id=chunk_id,
        audio_data=opus,
        sample_rate=16_000,
        channels=1,
        duration=duration,
    )
    return ClaimedChunk(
        chunk=chunk,
        range_id=f"range-{chunk_id}",
        clip_start_seconds=0.0,
        clip_end_seconds=duration,
        conversation_start_seconds=conversation_start,
    )


def first_pcm_sample(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        return np.frombuffer(wav_file.readframes(1), dtype="<i2")[0]


@pytest.mark.asyncio
async def test_capture_claim_splits_reconnect_gap_into_stable_ranges(monkeypatch):
    captured_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    chunks = [
        SimpleNamespace(
            id="chunk-1",
            capture_source_id="source-1",
            captured_at=captured_at,
            duration=10.0,
        ),
        SimpleNamespace(
            id="chunk-2",
            capture_source_id="source-1",
            captured_at=captured_at + timedelta(seconds=10),
            duration=10.0,
        ),
        SimpleNamespace(
            id="chunk-3",
            capture_source_id="source-1",
            captured_at=captured_at + timedelta(seconds=25),
            duration=10.0,
        ),
    ]

    ranges = audio_claims._build_capture_ranges(
        capture_session_id="session-1",
        capture_source_id="source-1",
        time_basis="received",
        chunks=chunks,
        started_at=captured_at,
        ended_at=captured_at + timedelta(seconds=35),
    )
    repeated = audio_claims._build_capture_ranges(
        capture_session_id="session-1",
        capture_source_id="source-1",
        time_basis="received",
        chunks=chunks,
        started_at=captured_at,
        ended_at=captured_at + timedelta(seconds=35),
    )

    assert [audio_range.chunk_ids for audio_range in ranges] == [
        ["chunk-1", "chunk-2"],
        ["chunk-3"],
    ]
    assert [audio_range.duration_seconds for audio_range in ranges] == [20.0, 10.0]
    assert [audio_range.range_id for audio_range in ranges] == [
        audio_range.range_id for audio_range in repeated
    ]

    monkeypatch.setattr(
        audio_claims, "load_chunks_by_id", AsyncMock(return_value=chunks)
    )
    resolved = await audio_claims.resolve_audio_ranges(ranges)
    assert [item.conversation_start_seconds for item in resolved] == [0.0, 10.0, 20.0]


@pytest.mark.asyncio
async def test_build_wav_from_pcm_runs_blocking_writer_off_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    writer_threads = []

    def fake_writer(pcm_data, sample_rate, channels, sample_width):
        writer_threads.append(threading.get_ident())
        return b"RIFF-test"

    monkeypatch.setattr(audio_chunk_utils, "_build_wav_bytes", fake_writer)

    result = await audio_chunk_utils.build_wav_from_pcm(b"pcm")

    assert result == b"RIFF-test"
    assert writer_threads and writer_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_full_claim_reconstruction_uses_canonical_chained_decode(monkeypatch):
    first = _claimed("chunk-1", b"first", conversation_start=0, duration=10)
    second = _claimed("chunk-2", b"second", conversation_start=10, duration=10)
    first.chunk.duration = 10
    second.chunk.duration = 10
    ranges = [SimpleNamespace(duration_seconds=20)]
    chained_decode = AsyncMock(return_value=b"canonical-pcm")

    monkeypatch.setattr(
        audio_chunk_utils,
        "resolve_audio_ranges",
        AsyncMock(return_value=[first, second]),
    )
    monkeypatch.setattr(
        audio_chunk_utils,
        "concatenate_chunks_to_pcm",
        chained_decode,
    )
    monkeypatch.setattr(
        audio_chunk_utils,
        "build_wav_from_pcm",
        AsyncMock(return_value=b"canonical-wav"),
    )

    result = await audio_chunk_utils.reconstruct_wav_from_claims(ranges)

    assert result == b"canonical-wav"
    chained_decode.assert_awaited_once_with([first.chunk, second.chunk])


@pytest.mark.asyncio
async def test_reconstruct_audio_ranges_reuses_decode_inside_one_compute_window(
    monkeypatch,
):
    pcm = np.repeat(np.arange(10, dtype="<i2") * 100, 16_000).tobytes()
    decode_calls = []

    async def decode(opus_data, sample_rate, channels):
        decode_calls.append((opus_data, sample_rate, channels))
        return pcm

    monkeypatch.setattr(
        audio_chunk_utils,
        "resolve_conversation_audio",
        AsyncMock(
            return_value=[
                _claimed("chunk-1", b"opus", conversation_start=0, duration=10)
            ]
        ),
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
    assert decode_calls == [(b"opus", 16_000, 1), (b"opus", 16_000, 1)]


@pytest.mark.asyncio
async def test_reconstruct_audio_ranges_decodes_disjoint_claims_as_islands(monkeypatch):
    decode_calls = []

    async def decode(opus_data, sample_rate, channels):
        decode_calls.append(opus_data)
        value = 100 if opus_data == b"first" else 200
        return np.full(4 * sample_rate, value, dtype="<i2").tobytes()

    resolved = [
        _claimed("chunk-1", b"first", conversation_start=0, duration=4),
        _claimed("chunk-2", b"second", conversation_start=4, duration=4),
    ]
    monkeypatch.setattr(
        audio_chunk_utils,
        "resolve_conversation_audio",
        AsyncMock(return_value=resolved),
    )
    monkeypatch.setattr(audio_chunk_utils, "decode_opus_to_pcm", decode)

    results = await audio_chunk_utils.reconstruct_audio_ranges(
        "conversation-1",
        [(1.0, 2.0), (5.0, 6.0)],
    )

    assert [first_pcm_sample(result) for result in results] == [100, 200]
    assert decode_calls == [b"first", b"second"]
