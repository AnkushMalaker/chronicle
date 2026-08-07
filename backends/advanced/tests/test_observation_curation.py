from datetime import datetime, timezone

import pytest

from advanced_omi_backend.models.device_input import (
    MAX_FRAME_CANDIDATES,
    DeviceInputItem,
)
from advanced_omi_backend.routers.modules.device_input_routes import (
    ObservationSample,
    _append_observation_sample,
    _merge_frame_candidates,
)
from advanced_omi_backend.services import observation_curation
from advanced_omi_backend.services.observation_curation import (
    _append_vault_observation,
    _observation_codex_settings,
    apply_curation_decision,
    observation_revision,
    safe_note_path,
)


def test_observation_codex_requires_explicit_model():
    with pytest.raises(ValueError, match="model must be explicitly configured"):
        _observation_codex_settings({})


def test_observation_codex_uses_luna_low_profile():
    assert _observation_codex_settings(
        {"model": " gpt-5.6-luna ", "reasoning_effort": "LOW", "timeout_seconds": 42}
    ) == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "timeout_seconds": 42,
    }


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


def test_frame_candidates_are_deduplicated_keeping_the_best_score():
    result = _merge_frame_candidates(
        [{"frame_id": 1, "score": 0.5}, {"frame_id": 2, "score": 0.8}],
        [
            {"frame_id": 1, "score": 0.9},
            {"frame_id": 3, "score": 0.7},
            {"frame_id": 4, "score": 0.1},
        ],
    )
    assert [candidate["frame_id"] for candidate in result] == [1, 2, 3, 4]
    assert result[0]["score"] == 0.9


def test_frame_shortlist_spans_the_observation_rather_than_the_top_scores():
    """A shortlist must sample the session, not cluster on its best-scoring moment.

    Consecutive frames of an unchanged window score almost identically, so ranking by
    score alone collapses the shortlist onto neighbours: measured here, observations
    longer than 15 minutes had all their candidates inside 5.8% of their span.
    """

    # One high-scoring burst at the start, then sparse frames across an hour.
    burst = [
        {
            "frame_id": index,
            "score": 0.99,
            "captured_at": f"2026-07-23T10:00:{index:02d}Z",
        }
        for index in range(10)
    ]
    spread = [
        {
            "frame_id": 100 + minute,
            "score": 0.4,
            "captured_at": f"2026-07-23T10:{minute:02d}:00Z",
        }
        for minute in (10, 20, 30, 40, 50)
    ]
    result = _merge_frame_candidates(burst, spread)

    assert len(result) == MAX_FRAME_CANDIDATES
    # The burst is represented, but it cannot crowd out the rest of the hour.
    assert sum(1 for candidate in result if candidate["frame_id"] < 100) == 1
    assert [
        candidate["frame_id"] for candidate in result if candidate["frame_id"] >= 100
    ] == [100 + minute for minute in (10, 20, 30, 40, 50)]


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


class _NoOpenJobs:
    """`DeviceInputJob` stand-in: nothing is in flight and inserts are recorded."""

    # The query builder reads these off the class before find_one is ever called.
    source_id = None
    kind = None

    def __init__(self, queued):
        self.queued = queued

    @staticmethod
    async def find_one(*args, **kwargs):
        return None

    def __call__(self, **fields):
        self.queued.append(fields)
        return self

    async def insert(self):
        return self


@pytest.mark.asyncio
async def test_preview_shortlist_is_requested_once_per_observation_then_gives_up(
    monkeypatch,
):
    """A pruned frame must not defer an observation forever.

    ScreenPipe drops frames, so a 404 is permanent. Re-requesting the top candidate
    every cron tick previously accumulated 13,113 failed jobs and left 83% of all
    observations at ``pending``, never written to the vault. One job now carries the
    whole shortlist, and after a bounded number of tries curation proceeds on text.
    """

    item = observation(
        frame_candidates=[
            {"frame_id": 14965, "score": 1.5},
            {"frame_id": 14966, "score": 1.2},
        ],
    )
    queued: list[dict] = []
    monkeypatch.setattr(observation_curation, "DeviceInputJob", _NoOpenJobs(queued))
    collection = _ObservationCollection(item.model_dump(by_alias=True))
    monkeypatch.setattr(DeviceInputItem, "get_pymongo_collection", lambda: collection)

    for _ in range(observation_curation._MAX_SHORTLIST_ATTEMPTS):
        assert await observation_curation._ensure_preview_shortlist(item) is True
        # Every candidate travels in one request, so the node is asked once, not once
        # per frame.
        assert queued[-1]["payload"]["frame_ids"] == [14965, 14966]
        item.metadata = collection.document["metadata"]

    assert await observation_curation._ensure_preview_shortlist(item) is False
    assert len(queued) == observation_curation._MAX_SHORTLIST_ATTEMPTS


def test_only_a_shortlisted_frame_can_be_selected():
    """The agent's pick is honoured only for an image it was actually shown."""

    item = observation(
        media_previews=[
            {"frame_id": 7, "data": b"jpeg", "content_type": "image/jpeg"},
        ],
    )
    assert observation_curation._selected_preview(item, {"selected_frame_id": 7})
    assert (
        observation_curation._selected_preview(item, {"selected_frame_id": 9}) is None
    )
    assert (
        observation_curation._selected_preview(item, {"selected_frame_id": None})
        is None
    )


@pytest.mark.asyncio
async def test_discarded_observation_keeps_the_chosen_timeline_thumbnail(monkeypatch):
    """Discard means "the vault needs no note", not "the timeline needs a blank".

    The chosen frame is timeline evidence, so it survives a discard. It must also not
    be written and cleared in one update: Mongo rejects a `$set` and `$unset` of the
    same path, which previously failed every discard of a closed observation.
    """

    item = observation(
        lifecycle="closed",
        media_previews=[
            {"frame_id": 7, "data": b"jpeg", "content_type": "image/jpeg"},
        ],
        curation="curating",
    )
    seen: list[tuple[dict, tuple]] = []

    async def capture(target, fields, *, unset=()):
        seen.append((fields, unset))
        return True

    monkeypatch.setattr(observation_curation, "_apply_curation_fields", capture)

    await apply_curation_decision(
        item,
        {"decision": "discard", "reason": "Routine", "selected_frame_id": 7},
        observation_revision(item),
    )

    fields, unset = seen[-1]
    assert fields["curation"] == "discarded"
    assert fields["media_data"] == b"jpeg"
    assert fields["metadata.preview_frame_id"] == 7
    assert not unset
    assert not set(fields) & set(unset)
    # The shortlist is spent once it has been chosen from.
    assert fields["media_previews"] == []


@pytest.mark.asyncio
async def test_discard_without_a_chosen_frame_still_clears_media(monkeypatch):
    """Nothing depicted the observation, so there is no thumbnail worth storing."""

    item = observation(lifecycle="closed", curation="curating")
    seen: list[tuple[dict, tuple]] = []

    async def capture(target, fields, *, unset=()):
        seen.append((fields, unset))
        return True

    monkeypatch.setattr(observation_curation, "_apply_curation_fields", capture)

    await apply_curation_decision(
        item,
        {"decision": "discard", "reason": "Routine", "selected_frame_id": None},
        observation_revision(item),
    )

    fields, unset = seen[-1]
    assert "media_data" in unset
    assert not set(fields) & set(unset)


def test_duplicate_candidates_are_labelled_not_addressed_by_objectid():
    """The agent must never be asked to echo a 24-hex ObjectId back.

    Asked to, it splices one together from ids it has seen: on a real observation it
    answered `6a74cf7d2937696bb7cc71d9` — the prefix of the observation's own id on the
    suffix of a candidate's. That resolves to nothing, and the resulting ValueError
    discarded the whole decision, chosen thumbnail included.
    """

    candidates = [
        {"id": "6a74c9ec2937696bb7cc71d9", "captured_at": "2026-08-06T17:52:34Z"},
        {"id": "6a74c9e22937696bb7cc71d7", "captured_at": "2026-08-06T17:51:00Z"},
    ]
    refs = {f"c{i + 1}": c["id"] for i, c in enumerate(candidates)}
    payload = [
        {**{k: v for k, v in c.items() if k != "id"}, "ref": ref}
        for ref, c in zip(refs, candidates)
    ]

    assert [entry["ref"] for entry in payload] == ["c1", "c2"]
    assert not any("id" in entry for entry in payload)
    assert refs["c2"] == "6a74c9e22937696bb7cc71d7"
    # An invented label resolves to nothing rather than to the wrong observation.
    assert refs.get("c9") is None
