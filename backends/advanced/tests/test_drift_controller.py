from types import SimpleNamespace

import pytest

from advanced_omi_backend.controllers import drift_controller as module


def _scan_fixture(monkeypatch, *, segments, centroids, assignments):
    version = SimpleNamespace(
        segments=segments,
        metadata={"cluster_centroids": centroids},
        created_at=None,
    )
    conversation = SimpleNamespace(
        conversation_id="conversation-1",
        title="Test conversation",
        user_id="user-1",
        active_transcript=version,
    )

    class Query:
        async def to_list(self):
            return [conversation]

    class Client:
        def __init__(self):
            self.calls = []

        async def reidentify_clusters(self, clusters, **kwargs):
            self.calls.append((clusters, kwargs))
            return {"assignments": assignments}

    client = Client()
    monkeypatch.setattr(
        module,
        "Conversation",
        SimpleNamespace(find=lambda _query: Query()),
    )
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: client)
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {
            "similarity_threshold": 0.5,
            "identify_margin": 0.1,
            "exclusive": True,
        },
    )
    return module, client


@pytest.mark.asyncio
async def test_drift_scan_replays_live_cluster_assignment_policy(monkeypatch):
    segment = SimpleNamespace(
        segment_type="speech",
        speaker="Alice",
        identified_as="Alice",
    )
    module, client = _scan_fixture(
        monkeypatch,
        segments=[segment],
        centroids={"Alice": [1.0, 0.0]},
        assignments={"Alice": {"name": "Alice", "id": "alice", "confidence": 0.8}},
    )

    result = await module.find_drift_conversations()

    assert result["total_drifted"] == 0
    assert client.calls == [
        (
            {"Alice": [1.0, 0.0]},
            {
                "user_id": "user-1",
                "similarity_threshold": 0.5,
                "identify_margin": 0.1,
                "exclusive": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_drift_scan_does_not_treat_human_overlay_as_old_unknown(monkeypatch):
    """A human overlay clears identified_as but keeps the visible corrected label."""

    segments = [
        SimpleNamespace(segment_type="speech", speaker="Alice", identified_as="Alice"),
        SimpleNamespace(segment_type="speech", speaker="Alice", identified_as=None),
    ]
    module, _ = _scan_fixture(
        monkeypatch,
        segments=segments,
        centroids={"Alice": [1.0, 0.0]},
        assignments={"Alice": {"name": "Alice", "id": "alice", "confidence": 0.8}},
    )

    result = await module.find_drift_conversations()

    assert result["total_drifted"] == 0


@pytest.mark.asyncio
async def test_drift_scan_reports_visible_labels_without_centroids_as_unverifiable(
    monkeypatch,
):
    segments = [
        SimpleNamespace(
            segment_type="speech", speaker="Roshan", identified_as="Roshan"
        ),
        SimpleNamespace(segment_type="speech", speaker="Vishnu", identified_as=None),
    ]
    module, _ = _scan_fixture(
        monkeypatch,
        segments=segments,
        centroids={"Noise": [1.0, 0.0]},
        assignments={},
    )

    result = await module.find_drift_conversations()

    assert result["total_drifted"] == 0
    assert result["unverifiable_segments"] == 2
    assert result["conversations_with_unverifiable_segments"] == 1


@pytest.mark.asyncio
async def test_drift_scan_detects_real_unknown_to_named_change(monkeypatch):
    segments = [
        SimpleNamespace(
            segment_type="speech", speaker="Unknown Speaker 1", identified_as=None
        ),
        SimpleNamespace(
            segment_type="speech", speaker="Unknown Speaker 1", identified_as=None
        ),
    ]
    module, _ = _scan_fixture(
        monkeypatch,
        segments=segments,
        centroids={"Unknown Speaker 1": [1.0, 0.0]},
        assignments={
            "Unknown Speaker 1": {
                "name": "Alice",
                "id": "alice",
                "confidence": 0.8,
            }
        },
    )

    result = await module.find_drift_conversations()

    assert result["total_drifted"] == 1
    assert result["drifted"][0]["drifted_segments"] == 2
    assert result["drifted"][0]["transitions"] == [
        {"from": None, "to": "Alice", "count": 2}
    ]


@pytest.mark.asyncio
async def test_drift_scan_detects_real_named_to_unknown_change(monkeypatch):
    segments = [
        SimpleNamespace(segment_type="speech", speaker="Alice", identified_as="Alice"),
        SimpleNamespace(segment_type="speech", speaker="Alice", identified_as="Alice"),
    ]
    module, _ = _scan_fixture(
        monkeypatch,
        segments=segments,
        centroids={"Alice": [1.0, 0.0]},
        assignments={},
    )

    result = await module.find_drift_conversations()

    assert result["total_drifted"] == 1
    assert result["drifted"][0]["transitions"] == [
        {"from": "Alice", "to": None, "count": 2}
    ]


@pytest.mark.asyncio
async def test_drift_scan_fails_instead_of_reporting_zero_when_service_is_down(
    monkeypatch,
):
    segment = SimpleNamespace(
        segment_type="speech", speaker="Alice", identified_as="Alice"
    )
    module, client = _scan_fixture(
        monkeypatch,
        segments=[segment],
        centroids={"Alice": [1.0, 0.0]},
        assignments={},
    )

    async def unavailable(*_args, **_kwargs):
        return {"error": "connection_failed", "assignments": {}}

    client.reidentify_clusters = unavailable

    with pytest.raises(RuntimeError, match="connection_failed"):
        await module.find_drift_conversations()
