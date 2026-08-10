import numpy as np
import pytest
import torch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from simple_speaker_recognition.api.routers import identification


class FakeAudioBackend:
    def __init__(self):
        self.batch_sizes = []

    def load_wave_bytes(self, content):
        if content == b"broken":
            raise ValueError("not audio")
        value = 1.0 if content == b"speaker-a" else 2.0
        return torch.full((1, 1, 800), value)

    def load_wave(self, path, *args):
        return torch.full((1, 1, 800), 1.0)

    async def async_diarize(self, path, **kwargs):
        return [
            {
                "speaker": "SPEAKER_00",
                "start": 0.0,
                "end": 1.0,
                "duration": 1.0,
            }
        ]

    async def async_embed(self, wave):
        return np.asarray([[1.0, 0.0]])

    async def async_embed_batch(self, waves):
        self.batch_sizes.append(len(waves))
        return np.asarray([[wave.mean().item(), 1.0] for wave in waves])


class FakeSpeakerDB:
    similarity_thr = 0.5

    def __init__(self):
        self.scalar_thresholds = []

    async def identify_batch_with_candidates(
        self, embeddings, user_id=None, similarity_threshold=None
    ):
        return [
            (
                embedding[0] < 1.5,
                (
                    {"id": "speaker-a", "name": "Speaker A", "user_id": user_id}
                    if embedding[0] < 1.5
                    else None
                ),
                0.9 if embedding[0] < 1.5 else 0.4,
                [],
            )
            for embedding in embeddings
        ]

    async def identify_with_candidates(
        self, embedding, user_id=None, similarity_threshold=None
    ):
        return (
            True,
            {"id": "speaker-a", "name": "Speaker A", "user_id": user_id},
            0.9,
            [],
        )

    async def identify(self, embedding, user_id=None, similarity_threshold=None):
        self.scalar_thresholds.append(similarity_threshold)
        return True, {"id": "speaker-a", "name": "Speaker A", "user_id": user_id}, 0.9


def test_identify_batch_preserves_order_and_item_errors(monkeypatch):
    audio_backend = FakeAudioBackend()
    database = FakeSpeakerDB()
    app = FastAPI()
    app.include_router(identification.router)
    app.dependency_overrides[identification.get_db] = lambda: database
    monkeypatch.setattr(identification, "get_audio_backend", lambda: audio_backend)

    with TestClient(app) as client:
        response = client.post(
            "/identify/batch",
            files=[
                ("files", ("a.wav", b"speaker-a", "audio/wav")),
                ("files", ("broken.wav", b"broken", "audio/wav")),
                ("files", ("unknown.wav", b"speaker-b", "audio/wav")),
            ],
            data={
                "segment_ids": ["first", "broken", "last"],
                "user_id": "1",
                "include_embeddings": "true",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert [result["segment_id"] for result in body["results"]] == [
        "first",
        "broken",
        "last",
    ]
    assert [result["status"] for result in body["results"]] == [
        "identified",
        "error",
        "unknown",
    ]
    assert body["results"][1]["error"] == "invalid_audio"
    assert body["results"][0]["embedding"] == [pytest.approx(1.0), pytest.approx(1.0)]
    assert body["batch"] == {"requested": 3, "processed": 2, "failed": 1}
    assert audio_backend.batch_sizes == [2]


def test_scalar_identify_does_not_dump_debug_audio_by_default(monkeypatch):
    audio_backend = FakeAudioBackend()
    database = FakeSpeakerDB()
    copied = []
    app = FastAPI()
    app.include_router(identification.router)
    app.dependency_overrides[identification.get_db] = lambda: database
    monkeypatch.delenv("SPEAKER_DEBUG_AUDIO_DUMP", raising=False)
    monkeypatch.setattr(identification, "get_audio_backend", lambda: audio_backend)
    monkeypatch.setattr(
        identification,
        "get_audio_info",
        lambda path: {"duration_seconds": 0.05},
    )
    monkeypatch.setattr(
        identification.shutil,
        "copy2",
        lambda source, destination: copied.append((source, destination)),
    )

    with TestClient(app) as client:
        response = client.post(
            "/identify",
            files={"file": ("clip.wav", b"audio", "audio/wav")},
            data={"user_id": "1", "similarity_threshold": "0.45"},
        )

    assert response.status_code == 200
    assert response.json()["speaker_name"] == "Speaker A"
    assert copied == []


def test_diarize_identify_passes_threshold_without_mutating_database(monkeypatch):
    audio_backend = FakeAudioBackend()
    database = FakeSpeakerDB()
    app = FastAPI()
    app.include_router(identification.router)
    app.dependency_overrides[identification.get_db] = lambda: database
    monkeypatch.setattr(identification, "get_audio_backend", lambda: audio_backend)
    monkeypatch.setattr(
        identification,
        "get_audio_info",
        lambda path: {"duration_seconds": 1.0},
    )

    with TestClient(app) as client:
        response = client.post(
            "/diarize-and-identify?similarity_threshold=0.8",
            files={"file": ("clip.wav", b"audio", "audio/wav")},
        )

    assert response.status_code == 200
    assert database.scalar_thresholds == [0.8]
    assert database.similarity_thr == 0.5
