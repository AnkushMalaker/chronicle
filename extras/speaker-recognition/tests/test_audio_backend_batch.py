import asyncio

import numpy as np
import torch
from simple_speaker_recognition.core.audio_backend import AudioBackend


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


def test_local_speaker_centroids_skip_degenerate_embedding_without_aborting_chunk():
    backend = AudioBackend.__new__(AudioBackend)
    backend.load_wave = lambda _path, start, _end: torch.tensor([start])

    def embed(wave):
        if float(wave.item()) == 0.0:
            raise ValueError("Embedding model returned a non-finite or zero vector")
        return np.array([3.0, 4.0], dtype=np.float32)

    backend.embed = embed

    centroids = asyncio.run(
        backend._embed_local_speakers(
            "unused.wav",
            [
                {"speaker": "silent", "start": 0.0, "end": 2.0},
                {"speaker": "speech", "start": 2.0, "end": 4.0},
            ],
        )
    )

    assert set(centroids) == {"speech"}
    np.testing.assert_allclose(centroids["speech"], np.array([0.6, 0.8]))
