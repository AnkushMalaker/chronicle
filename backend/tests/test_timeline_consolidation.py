from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.models.timeline import EpisodeRevisionRef, TimelineDaySnapshot
from backend.services.timeline import consolidation as consolidation_module
from backend.services.timeline.consolidation import (
    _validated_suggestions,
    prefetch_consolidation_horizon,
    queue_day_consolidation,
    render_day_tape_png,
    resolve_day_consolidation,
    suggest_episode_consolidation,
)
from backend.workers import timeline_jobs

BASE = datetime(2026, 2, 15, tzinfo=timezone.utc)


def episode(index: int, minute: int):
    return SimpleNamespace(
        episode_id=f"episode-{index}",
        episode_key=f"key-{index}",
        revision=3,
        started_at=BASE + timedelta(minutes=minute),
        ended_at=BASE + timedelta(minutes=minute + 10),
        activity_mode="foreground",
        memory_policy="auto",
        confirmed_fields=[],
        evidence_refs=[],
        conversational=False,
        title=f"Episode {index}",
    )


def test_day_tape_is_a_real_png_with_one_row_per_episode():
    image = render_day_tape_png([episode(1, 10), episode(2, 40)], "Asia/Kolkata")

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 500


def test_suggestions_map_labels_to_exact_revisions_and_allow_nonconsecutive_groups():
    episodes = [episode(1, 10), episode(2, 40), episode(3, 70), episode(4, 100)]
    raw = {
        "suggestions": [
            {
                "episode_labels": ["E01", "E02"],
                "title": "One task",
                "reason": "Same objective",
                "confidence": 0.9,
            },
            {
                "episode_labels": ["E02", "E03"],
                "title": "Overlapping",
                "reason": "Must be dropped",
                "confidence": 0.8,
            },
            {
                "episode_labels": ["E03", "E05"],
                "title": "Unknown label",
                "reason": "Must be dropped",
                "confidence": 0.7,
            },
        ]
    }

    result = _validated_suggestions(raw, episodes, "a" * 64)

    assert len(result) == 1
    assert result[0].episode_ids == ["episode-1", "episode-2"]
    assert result[0].title == "One task"
    assert result[0].member_revisions == [
        EpisodeRevisionRef(episode_key="key-1", revision=3),
        EpisodeRevisionRef(episode_key="key-2", revision=3),
    ]


async def test_grouping_rejects_model_output_without_suggestions(monkeypatch):
    episodes = [episode(1, 10), episode(2, 40)]
    for item in episodes:
        item.related_conversation_ids = []
        item.summary = ""
        item.kind = "work"
        item.entities = []

    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
        )
    )
    operation = SimpleNamespace(
        model_def=SimpleNamespace(name="qwen", capabilities=["vision"]),
        prepare_messages=lambda messages: messages,
        get_client=lambda is_async: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
        to_api_params=lambda: {},
    )
    monkeypatch.setattr(
        consolidation_module,
        "get_models_registry",
        lambda: SimpleNamespace(get_llm_operation=lambda _name: operation),
    )
    monkeypatch.setattr(
        consolidation_module,
        "render_day_tape_png",
        lambda _episodes, _timezone: b"png",
    )

    with pytest.raises(RuntimeError, match="suggestions array"):
        await suggest_episode_consolidation(episodes, {}, "Asia/Kolkata", "a" * 64)

    assert create.await_count == 2


async def test_grouping_accepts_json_from_a_markdown_fence(monkeypatch):
    episodes = [episode(1, 10), episode(2, 40)]
    for item in episodes:
        item.related_conversation_ids = []
        item.summary = ""
        item.kind = "work"
        item.entities = []

    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='```json\n{"suggestions": []}\n```')
                )
            ]
        )
    )
    operation = SimpleNamespace(
        model_def=SimpleNamespace(name="qwen", capabilities=["vision"]),
        prepare_messages=lambda messages: messages,
        get_client=lambda is_async: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
        to_api_params=lambda: {},
    )
    monkeypatch.setattr(
        consolidation_module,
        "get_models_registry",
        lambda: SimpleNamespace(get_llm_operation=lambda _name: operation),
    )
    monkeypatch.setattr(
        consolidation_module,
        "render_day_tape_png",
        lambda _episodes, _timezone: b"png",
    )

    result = await suggest_episode_consolidation(episodes, {}, "Asia/Kolkata", "a" * 64)

    assert result["suggestions"] == []
    assert create.await_count == 1


@pytest.mark.parametrize("synthesis_fails", [True, False])
@pytest.mark.parametrize("finalize", [True, False])
async def test_resolution_persists_groups_and_accept_reject_training_decisions(
    monkeypatch,
    finalize,
    synthesis_fails,
):
    episodes = [episode(1, 10), episode(2, 40), episode(3, 70), episode(4, 100)]
    recorded = {}

    class Cursor:
        def sort(self, *_args):
            return self

        async def to_list(self):
            return episodes

    class EpisodeModel:
        user_id = "user_id"

        @staticmethod
        def find(*_args, **_kwargs):
            return Cursor()

    class Collection:
        async def update_one(self, query, update):
            recorded.update(query=query, update=update)
            return SimpleNamespace(modified_count=1)

    class DayModel:
        @staticmethod
        def get_pymongo_collection():
            return Collection()

    async def synthesize(_members):
        if synthesis_fails:
            raise RuntimeError("LLM returned empty content (finish_reason=length)")
        return SimpleNamespace(title="Combined task", summary="One coherent task.")

    monkeypatch.setattr(consolidation_module, "TimelineEpisode", EpisodeModel)
    monkeypatch.setattr(consolidation_module, "TimelineDay", DayModel)
    monkeypatch.setattr(
        consolidation_module, "synthesize_merged_episode_account", synthesize
    )
    monkeypatch.setattr(
        consolidation_module,
        "_publish_group_revisions",
        AsyncMock(return_value="b" * 64),
    )
    refs = [
        EpisodeRevisionRef(episode_key=item.episode_key, revision=item.revision)
        for item in episodes
    ]
    day = SimpleNamespace(
        id="day-one",
        user_id="user-one",
        local_date=date(2026, 2, 15),
        timezone="Asia/Kolkata",
        current_snapshot=TimelineDaySnapshot(
            snapshot_id="a" * 64,
            episode_revisions=refs,
            evidence_state_hash="c" * 64,
        ),
        current_snapshot_id="a" * 64,
        consolidation_snapshot_id="a" * 64,
        consolidation_state="ready",
        consolidation_model="qwen",
        consolidation_suggestions=[
            {
                "suggestion_id": "group:first",
                "episode_ids": ["episode-1", "episode-2"],
                "member_revisions": [item.model_dump(mode="json") for item in refs[:2]],
                "source_snapshot_id": "a" * 64,
                "title": "First task",
                "reason": "Same task",
                "confidence": 0.9,
            },
            {
                "suggestion_id": "group:second",
                "episode_ids": ["episode-3", "episode-4"],
                "member_revisions": [item.model_dump(mode="json") for item in refs[2:]],
                "source_snapshot_id": "a" * 64,
                "title": "Second task",
                "reason": "Possibly related",
                "confidence": 0.7,
            },
        ],
        semantic_group_history=[],
    )

    if synthesis_fails:
        with pytest.raises(
            consolidation_module.ConsolidationSynthesisError,
            match="No groupings were saved",
        ):
            await resolve_day_consolidation(
                day, ["group:first", "group:second"], finalize=finalize
            )
        consolidation_module._publish_group_revisions.assert_not_awaited()
        assert recorded == {}
        assert day.consolidation_state == "ready"
        assert len(day.consolidation_suggestions) == 2
        return

    groups = await resolve_day_consolidation(day, ["group:first"], finalize=finalize)

    assert len(groups) == 1
    assert groups[0].episode_ids == ["episode-1", "episode-2"]
    if not finalize:
        saved = recorded["update"]["$set"]
        assert saved["consolidation_state"] == "ready"
        assert saved["consolidation_snapshot_id"] == "b" * 64
        assert [
            item["suggestion_id"] for item in saved["consolidation_suggestions"]
        ] == ["group:second"]
        assert saved["consolidation_suggestions"][0]["source_snapshot_id"] == "b" * 64
        assert "$push" not in recorded["update"]
        day.current_snapshot = day.current_snapshot.model_copy(
            update={"snapshot_id": "b" * 64}
        )
        day.current_snapshot_id = "b" * 64
        day.consolidation_snapshot_id = "b" * 64
        day.consolidation_suggestions = saved["consolidation_suggestions"]
        consolidation_module._publish_group_revisions.return_value = "c" * 64
        next_groups = await resolve_day_consolidation(
            day, ["group:second"], finalize=False
        )
        assert next_groups[0].episode_ids == ["episode-3", "episode-4"]
        assert recorded["update"]["$set"]["consolidation_state"] == "resolved"
        assert recorded["update"]["$set"]["consolidation_snapshot_id"] == "c" * 64
        assert "$push" not in recorded["update"]
        return
    assert recorded["update"]["$set"]["consolidation_state"] == "resolved"
    decisions = recorded["update"]["$push"]["review_decisions"]["$each"]
    assert [item["action"] for item in decisions] == ["grouping_reject"]
    assert all(item["before"]["suggestion_id"] for item in decisions)


async def test_pregeneration_job_calls_the_snapshot_fenced_generator(monkeypatch):
    observed = {}

    async def fake_generate(user_id, local_date, timezone_name, snapshot_id):
        observed.update(
            user_id=user_id,
            local_date=local_date,
            timezone=timezone_name,
            snapshot_id=snapshot_id,
        )
        return {"state": "ready", "suggestions": []}

    monkeypatch.setattr(timeline_jobs, "generate_day_consolidation", fake_generate)

    result = await timeline_jobs.generate_timeline_consolidation_job.__wrapped__(
        "user-1", "2026-02-15", "Asia/Kolkata", "run-1"
    )

    assert result["state"] == "ready"
    assert observed == {
        "user_id": "user-1",
        "local_date": date(2026, 2, 15),
        "timezone": "Asia/Kolkata",
        "snapshot_id": "run-1",
    }


async def test_manual_grouping_request_enqueues_the_registered_worker(monkeypatch):
    queue = SimpleNamespace(enqueue=Mock())
    monkeypatch.setattr("backend.controllers.queue_controller.default_queue", queue)
    monkeypatch.setattr(
        "backend.services.timeline.executor.settings_dict",
        lambda: {"consolidation": {"timeout_seconds": 345}},
    )

    result = await queue_day_consolidation(
        "user-1", date(2026, 2, 19), "Asia/Kolkata", "run-1"
    )

    assert result == {
        "state": "queued",
        "snapshot_id": "run-1",
        "model": None,
        "suggestions": [],
        "error": None,
        "generated_at": None,
    }
    args, kwargs = queue.enqueue.call_args
    assert args[1:] == ("user-1", "2026-02-19", "Asia/Kolkata", "run-1")
    assert kwargs == {"job_timeout": 345}


async def test_prefetch_job_keeps_configured_oldest_days_ready(monkeypatch):
    days = [
        {
            "_id": f"day-{index}",
            "user_id": "user-1",
            "local_date": datetime(2026, 2, 15 + index),
            "timezone": "Asia/Kolkata",
            "current_snapshot_id": f"snapshot-{index}",
            "consolidation_state": "ready" if index == 0 else "",
        }
        for index in range(5)
    ]

    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, count):
            self.count = count
            return self

        async def to_list(self, *, length):
            return days[: min(self.count, length)]

    collection = SimpleNamespace(
        distinct=AsyncMock(return_value=["user-1"]),
        find=Mock(return_value=Cursor()),
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
    )
    queue = SimpleNamespace(enqueue=Mock())
    monkeypatch.setattr(
        "backend.services.timeline.executor.settings_dict",
        lambda: {"consolidation": {"pregenerate": True, "prefetch_days": 5}},
    )
    monkeypatch.setattr("backend.controllers.queue_controller.default_queue", queue)
    monkeypatch.setattr(
        "backend.models.timeline.TimelineDay.get_pymongo_collection",
        lambda: collection,
    )

    result = await prefetch_consolidation_horizon()

    assert result == {"users": 1, "considered": 5, "queued": 4, "failed": 0}
    assert queue.enqueue.call_count == 4


async def test_generation_excludes_accepted_members_and_capture_only_ambient(
    monkeypatch,
):
    items = [episode(i, i * 10) for i in range(1, 7)]
    for item in items:
        item.related_conversation_ids = []
        item.evidence_refs = []
    items[1].activity_mode = "ambient"
    for item in items:
        item.confirmed_fields = []
    items[1].evidence_refs = [
        SimpleNamespace(kind="audio_span", metadata={"state": "no_speech"}),
        SimpleNamespace(kind="capture_gap"),
    ]
    items[2].activity_mode = "background"
    items[2].evidence_refs = [
        SimpleNamespace(kind="transcript", role="uncertain", excerpt="Real speech")
    ]
    # Generated work labels must not promote television dialogue or empty input.
    items[4].evidence_refs = [
        SimpleNamespace(kind="transcript", role="media_content", excerpt="TV dialogue")
    ]
    items[5].evidence_refs = [
        SimpleNamespace(kind="transcript", role="uncertain", excerpt=None)
    ]
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1))
    )
    monkeypatch.setattr(
        consolidation_module.TimelineDay, "get_pymongo_collection", lambda: collection
    )
    monkeypatch.setattr(
        consolidation_module.TimelineDay,
        "find_one",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        consolidation_module, "snapshot_episodes", AsyncMock(return_value=items)
    )
    monkeypatch.setattr(
        consolidation_module,
        "active_semantic_groups",
        lambda day: [SimpleNamespace(episode_ids=[items[0].episode_id])],
    )
    suggest = AsyncMock(return_value={"model": "test", "suggestions": []})
    monkeypatch.setattr(consolidation_module, "suggest_episode_consolidation", suggest)
    result = await consolidation_module.generate_day_consolidation(
        "user-1", date(2026, 2, 15), "Asia/Kolkata", "snapshot"
    )
    assert result["state"] == "ready"
    assert suggest.call_args.args[0] == items[2:4]
