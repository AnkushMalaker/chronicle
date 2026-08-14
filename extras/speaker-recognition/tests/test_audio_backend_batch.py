import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from pyannote.core import Annotation, Segment

from simple_speaker_recognition.core.audio_backend import AudioBackend


class StaticDiarizer:
    def __init__(self, annotation):
        self.annotation = annotation
        self.instantiate_calls = []

    def instantiate(self, parameters):
        self.instantiate_calls.append(parameters)

    def __call__(self, _path, **_kwargs):
        return self.annotation


class RecordingEmbedder:
    dimension = 2

    def __init__(self):
        self.batch_shapes = []

    def __call__(self, waveforms):
        self.batch_shapes.append(tuple(waveforms.shape))
        means = waveforms.mean(dim=(1, 2)).cpu().numpy()
        # PyAnnote's PretrainedSpeakerEmbedding returns (batch, 1, dimension).
        return np.stack([means, np.ones_like(means)], axis=1)[:, None, :]


def test_embed_batch_groups_similar_lengths_and_preserves_input_order():
    backend = AudioBackend.__new__(AudioBackend)
    backend.device = torch.device("cpu")
    backend.embedder = RecordingEmbedder()
    waves = [
        torch.full((1, 1, 500), 3.0),
        torch.full((1, 1, 1_000), 1.0),
        torch.full((1, 1, 550), 2.0),
    ]

    embeddings = backend.embed_batch(
        waves,
        max_batch_size=8,
        max_padding_ratio=1.25,
    )

    assert backend.embedder.batch_shapes == [(2, 1, 550), (1, 1, 1_000)]
    assert embeddings.shape == (3, 2)
    np.testing.assert_allclose(np.linalg.norm(embeddings, axis=1), 1.0)
    assert embeddings[0, 0] > embeddings[2, 0] > embeddings[1, 0]


def test_async_embed_reuses_one_dedicated_model_worker():
    backend = AudioBackend.__new__(AudioBackend)
    backend._embedding_executor = ThreadPoolExecutor(max_workers=1)
    worker_threads = []

    def embed(_wave):
        worker_threads.append(threading.get_ident())
        time.sleep(0.02)
        return np.array([[1.0, 0.0]], dtype=np.float32)

    backend.embed = embed

    async def run_calls():
        await asyncio.gather(*(backend.async_embed(torch.zeros(400)) for _ in range(4)))

    try:
        asyncio.run(run_calls())
    finally:
        backend._embedding_executor.shutdown()

    assert len(set(worker_threads)) == 1


def test_cancelled_executor_job_waits_for_native_worker_to_settle():
    executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    finished = threading.Event()

    def native_work():
        started.set()
        time.sleep(0.03)
        finished.set()

    async def cancel_work():
        task = asyncio.create_task(
            AudioBackend._run_executor_job(executor, native_work)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_work())
    finally:
        executor.shutdown()

    assert finished.is_set()


def test_local_speaker_centroids_skip_degenerate_embedding_without_aborting_chunk():
    backend = AudioBackend.__new__(AudioBackend)
    backend._embedding_executor = ThreadPoolExecutor(max_workers=1)
    backend.load_wave = lambda _path, start, _end: torch.tensor([start])

    def embed(wave):
        if float(wave.item()) == 0.0:
            raise ValueError("Embedding model returned a non-finite or zero vector")
        return np.array([3.0, 4.0], dtype=np.float32)

    backend.embed = embed

    try:
        centroids = asyncio.run(
            backend._embed_local_speakers(
                "unused.wav",
                [
                    {"speaker": "silent", "start": 0.0, "end": 2.0},
                    {"speaker": "speech", "start": 2.0, "end": 4.0},
                ],
            )
        )
    finally:
        backend._embedding_executor.shutdown()

    assert set(centroids) == {"speech"}
    np.testing.assert_allclose(centroids["speech"], np.array([0.6, 0.8]))


def test_async_diarize_serializes_gpu_requests():
    backend = AudioBackend.__new__(AudioBackend)
    backend._diarization_lock = asyncio.Lock()
    active = 0
    peak = 0

    async def fake_locked(_path, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    backend._async_diarize_locked = fake_locked

    async def no_cleanup(_reason):
        return None

    backend.async_release_cuda_cache = no_cleanup

    async def run_calls():
        await asyncio.gather(
            backend.async_diarize("first.wav"),
            backend.async_diarize("second.wav"),
        )

    asyncio.run(run_calls())

    assert peak == 1


def test_async_diarize_releases_cuda_cache_after_failure():
    backend = AudioBackend.__new__(AudioBackend)
    backend._diarization_lock = asyncio.Lock()
    cleanup_reasons = []

    async def fail(_path, **_kwargs):
        raise RuntimeError("inference failed")

    async def record_cleanup(reason):
        cleanup_reasons.append(reason)

    backend._async_diarize_locked = fail
    backend.async_release_cuda_cache = record_cleanup

    with pytest.raises(RuntimeError, match="inference failed"):
        asyncio.run(backend.async_diarize("unused.wav"))

    assert cleanup_reasons == ["diarization request"]


def test_release_cuda_cache_logs_allocator_change(monkeypatch, caplog):
    backend = AudioBackend.__new__(AudioBackend)
    backend.device = torch.device("cuda")
    synchronized = []
    emptied = []
    reserved = iter([500 * 1024 * 1024, 200 * 1024 * 1024])

    monkeypatch.setattr(torch.cuda, "synchronize", synchronized.append)
    monkeypatch.setattr(
        torch.cuda, "memory_allocated", lambda _device: 100 * 1024 * 1024
    )
    monkeypatch.setattr(torch.cuda, "memory_reserved", lambda _device: next(reserved))
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: emptied.append(True))

    with caplog.at_level("INFO"):
        backend.release_cuda_cache("test request")

    assert synchronized == [backend.device]
    assert emptied == [True]
    assert "allocated 100.0 -> 100.0 MiB" in caplog.text
    assert "reserved 500.0 -> 200.0 MiB; returned 300.0 MiB" in caplog.text


def test_async_diarize_rejects_nonpositive_neural_window():
    backend = AudioBackend.__new__(AudioBackend)
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 2400.0)},
    )()

    with pytest.raises(ValueError, match="max_duration must be greater than zero"):
        asyncio.run(backend._async_diarize_locked("unused.wav", max_duration=0.0))


def test_async_diarize_logs_exact_chunk_count_for_multiple_of_window(caplog):
    backend = AudioBackend.__new__(AudioBackend)
    backend._diarization_executor = ThreadPoolExecutor(max_workers=2)
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 2400.0)},
    )()
    backend.load_wave = lambda _path, start, end: torch.zeros((1, 1, 400))
    backend.diarize = lambda *_args: []

    async def no_centroids(_path, _segments):
        return {}

    backend._embed_local_speakers = no_centroids

    try:
        with caplog.at_level("INFO"):
            segments = asyncio.run(
                backend._async_diarize_locked("unused.wav", max_duration=1200.0)
            )
    finally:
        backend._diarization_executor.shutdown()

    assert segments == []
    assert "Using 2 chunks with 5.0s overlap" in caplog.text


def test_async_diarize_runs_neural_windows_with_bounded_parallelism():
    backend = AudioBackend.__new__(AudioBackend)
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 3600.0)},
    )()
    backend.load_wave = lambda _path, start, end: torch.zeros((1, 1, 400))
    active = 0
    peak = 0

    async def diarize_window(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    async def no_centroids(_path, _segments):
        return {}

    backend._diarize_window = diarize_window
    backend._embed_local_speakers = no_centroids

    segments = asyncio.run(
        backend._async_diarize_locked(
            "unused.wav", max_duration=1200.0, max_concurrent_chunks=2
        )
    )

    assert segments == []
    assert peak == 2


def test_chunk_failure_stops_queued_windows_and_waits_for_active_worker():
    backend = AudioBackend.__new__(AudioBackend)
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 1800.0)},
    )()
    backend.load_wave = lambda _path, start, end: torch.zeros((1, 1, 400))
    started = 0
    active_worker_finished = False

    async def diarize_window(*_args):
        nonlocal started, active_worker_finished
        started += 1
        if started == 1:
            while started < 2:
                await asyncio.sleep(0)
            raise RuntimeError("first window failed")
        await asyncio.sleep(0.03)
        active_worker_finished = True
        return []

    async def no_centroids(_path, _segments):
        return {}

    backend._diarize_window = diarize_window
    backend._embed_local_speakers = no_centroids

    with pytest.raises(RuntimeError, match="first window failed"):
        asyncio.run(
            backend._async_diarize_locked(
                "unused.wav", max_duration=600.0, max_concurrent_chunks=2
            )
        )

    assert started == 2
    assert active_worker_finished is True


def test_diarize_clips_pyannote_frame_overhang_to_decoded_audio_duration():
    annotation = Annotation()
    annotation[Segment(9.5, 10.156)] = "SPEAKER_00"
    backend = AudioBackend.__new__(AudioBackend)
    backend.diar = StaticDiarizer(annotation)
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 10.0)},
    )()

    segments = backend.diarize("unused.wav")

    assert segments == [
        {
            "start": 9.5,
            "end": 10.0,
            "speaker": "SPEAKER_00",
            "duration": 0.5,
        }
    ]


def test_diarize_uses_exclusive_timeline_without_gap_fill_overlap():
    overlapping = Annotation()
    overlapping[Segment(0.0, 3.0)] = "RAW_SPEAKER"
    exclusive = Annotation()
    exclusive[Segment(0.0, 1.0)] = "SPEAKER_00"
    exclusive[Segment(1.5, 2.5)] = "SPEAKER_00"
    backend = AudioBackend.__new__(AudioBackend)
    backend.diar = StaticDiarizer(
        SimpleNamespace(
            speaker_diarization=overlapping,
            exclusive_speaker_diarization=exclusive,
        )
    )
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 3.0)},
    )()

    segments = backend.diarize("unused.wav", collar=2.0)

    assert segments == [
        {
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPEAKER_00",
            "duration": 1.0,
        },
        {
            "start": 1.5,
            "end": 2.5,
            "speaker": "SPEAKER_00",
            "duration": 1.0,
        },
    ]


def test_diarize_does_not_mutate_pipeline_during_parallel_inference():
    backend = AudioBackend.__new__(AudioBackend)
    backend.diar = StaticDiarizer(
        SimpleNamespace(
            speaker_diarization=Annotation(),
            exclusive_speaker_diarization=Annotation(),
        )
    )
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 3.0)},
    )()

    backend.diarize("unused.wav")

    assert backend.diar.instantiate_calls == []


def test_diarize_rejects_nonzero_gap_fill_for_exclusive_timeline():
    backend = AudioBackend.__new__(AudioBackend)
    backend.diar = StaticDiarizer(Annotation())
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 3.0)},
    )()

    with pytest.raises(ValueError, match="min_duration_off must be 0"):
        backend.diarize("unused.wav", min_duration_off=1.5)

    assert backend.diar.instantiate_calls == []


def test_diarize_rejects_overlapping_exclusive_timeline():
    exclusive = Annotation()
    exclusive[Segment(0.0, 2.0)] = "SPEAKER_00"
    exclusive[Segment(1.0, 3.0)] = "SPEAKER_01"
    backend = AudioBackend.__new__(AudioBackend)
    backend.diar = StaticDiarizer(
        SimpleNamespace(
            speaker_diarization=Annotation(),
            exclusive_speaker_diarization=exclusive,
        )
    )
    backend.loader = type(
        "StaticDurationLoader",
        (),
        {"get_duration": staticmethod(lambda _path: 3.0)},
    )()

    with pytest.raises(ValueError, match="overlapping turns"):
        backend.diarize("unused.wav")


def test_long_window_overlap_is_context_not_duplicate_output():
    segments = [
        {"start": 1198.0, "end": 1203.0, "speaker": "SPEAKER_00", "duration": 5.0},
        {"start": 1200.0, "end": 1204.0, "speaker": "SPEAKER_01", "duration": 4.0},
        {"start": 1199.5, "end": 1200.0, "speaker": "SPEAKER_01", "duration": 0.5},
    ]

    owned = AudioBackend._clip_segments_to_core(segments, 1200.0)

    assert owned == [
        {
            "start": 1198.0,
            "end": 1200.0,
            "speaker": "SPEAKER_00",
            "duration": 2.0,
        },
        {
            "start": 1199.5,
            "end": 1200.0,
            "speaker": "SPEAKER_01",
            "duration": 0.5,
        },
    ]
