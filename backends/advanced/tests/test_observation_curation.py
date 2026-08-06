from datetime import datetime, timezone

import pytest

from advanced_omi_backend.models.device_input import DeviceInputItem
from advanced_omi_backend.routers.modules.device_input_routes import (
    ObservationSample,
    _append_observation_sample,
    _merge_frame_candidates,
)
from advanced_omi_backend.services.observation_curation import (
    _append_vault_observation,
    apply_curation_decision,
    observation_revision,
    safe_note_path,
)


def observation(**overrides):
    values = {
        "user_id": "user-1",
        "source_id": "screenpipe-1",
        "kind": "observation",
        "source_item_id": "observation:10",
        "captured_at": datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc),
        "metadata": {"app_name": "Code", "window_name": "collector.py"},
        "lifecycle": "open",
        "curation": "pending",
    }
    values.update(overrides)
    return DeviceInputItem.model_construct(**values)


def sample(when: str, fingerprint: str = "a" * 64):
    return ObservationSample(
        captured_at=when,
        elapsed_seconds=10,
        capture_trigger="typing_pause",
        text="implemented the observation lifecycle",
        content_fingerprint=fingerprint,
        frame_id=10,
    )


def test_samples_are_idempotent_by_fingerprint_and_timestamp():
    item = observation()
    assert _append_observation_sample(item, sample("2026-07-23T10:00:10Z"))
    assert not _append_observation_sample(item, sample("2026-07-23T10:00:10Z"))
    assert _append_observation_sample(item, sample("2026-07-23T10:03:00Z"))
    assert len(item.samples) == 2


def test_frame_candidates_are_ranked_deduplicated_and_bounded():
    result = _merge_frame_candidates(
        [{"frame_id": 1, "score": 0.5}, {"frame_id": 2, "score": 0.8}],
        [
            {"frame_id": 1, "score": 0.9},
            {"frame_id": 3, "score": 0.7},
            {"frame_id": 4, "score": 0.1},
        ],
    )
    assert [candidate["frame_id"] for candidate in result] == [1, 2, 3]


def test_revision_changes_for_new_sample_and_close():
    item = observation()
    first = observation_revision(item)
    _append_observation_sample(item, sample("2026-07-23T10:00:10Z"))
    second = observation_revision(item)
    item.lifecycle = "closed"
    third = observation_revision(item)
    assert len({first, second, third}) == 3


def test_note_path_rejects_traversal_and_conversation_notes(tmp_path):
    with pytest.raises(ValueError):
        safe_note_path(tmp_path, "../outside.md", datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        safe_note_path(tmp_path, "Conversations/fake.md", datetime.now(timezone.utc))


def test_vault_append_is_revision_idempotent(tmp_path):
    item = observation(
        related_conversation_ids=["conversation-1"],
    )
    decision = {
        "decision": "text_update",
        "title": "Chronicle implementation",
        "summary": "Implemented event-driven screen observations.",
        "facts": ["Short meaningful app switches are retained."],
    }
    path = _append_vault_observation(item, decision, "revision-1", tmp_path, None)
    _append_vault_observation(item, decision, "revision-1", tmp_path, None)
    content = (tmp_path / path).read_text(encoding="utf-8")
    assert content.count("<!-- observation:") == 1
    assert "[[Conversations/conversation-1]]" in content


class _UpdateResult:
    def __init__(self, matched_count: int):
        self.matched_count = matched_count


class _ObservationCollection:
    def __init__(self, document):
        self.document = document

    async def update_one(self, query, update):
        if any(self.document.get(key) != value for key, value in query.items()):
            return _UpdateResult(0)
        for key, value in update.get("$set", {}).items():
            target = self.document
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        for key in update.get("$unset", {}):
            self.document.pop(key, None)
        return _UpdateResult(1)


@pytest.mark.asyncio
async def test_stale_curation_cannot_overwrite_newer_observation_lifecycle(monkeypatch):
    stale = observation(
        metadata={"app_name": "steam_app_1466860", "frame_count": 261},
        samples=[sample("2026-07-23T10:05:00Z").model_dump(mode="json")],
        curation="curating",
    )
    live = stale.model_dump(by_alias=True)
    live.update(
        {
            "lifecycle": "closed",
            "ended_at": datetime(2026, 7, 23, 10, 40, tzinfo=timezone.utc),
            "curation": "pending",
            "samples": [
                *live["samples"],
                sample("2026-07-23T10:40:00Z", "b" * 64).model_dump(mode="json"),
            ],
        }
    )
    live["metadata"] = {**live["metadata"], "frame_count": 632}
    collection = _ObservationCollection(live)
    monkeypatch.setattr(DeviceInputItem, "get_pymongo_collection", lambda: collection)

    async def replace_document(item):
        collection.document = item.model_dump(by_alias=True)
        return item

    monkeypatch.setattr(DeviceInputItem, "save", replace_document)

    applied = await apply_curation_decision(
        stale,
        {"decision": "discard", "reason": "Routine gameplay"},
        observation_revision(stale),
    )

    assert applied is False
    assert collection.document["lifecycle"] == "closed"
    assert collection.document["ended_at"] == datetime(
        2026, 7, 23, 10, 40, tzinfo=timezone.utc
    )
    assert collection.document["metadata"]["frame_count"] == 632
    assert len(collection.document["samples"]) == 2
    assert collection.document["curation"] == "pending"


@pytest.mark.asyncio
async def test_current_curation_updates_only_curation_owned_fields(monkeypatch):
    item = observation(
        metadata={"app_name": "steam_app_1466860", "frame_count": 261},
        samples=[sample("2026-07-23T10:05:00Z").model_dump(mode="json")],
        curation="curating",
    )
    live = item.model_dump(by_alias=True)
    # Frame counters and candidate metadata are ingestion-owned and intentionally do
    # not change the semantic curation revision.
    live["metadata"] = {**live["metadata"], "frame_count": 632}
    collection = _ObservationCollection(live)
    monkeypatch.setattr(DeviceInputItem, "get_pymongo_collection", lambda: collection)

    async def replace_document(document):
        collection.document = document.model_dump(by_alias=True)
        return document

    monkeypatch.setattr(DeviceInputItem, "save", replace_document)

    applied = await apply_curation_decision(
        item,
        {"decision": "discard", "reason": "Routine gameplay"},
        observation_revision(item),
    )

    assert applied is True
    assert collection.document["metadata"]["frame_count"] == 632
    assert collection.document["curation"] == "discarded"
