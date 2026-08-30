from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.services.timeline import consolidation as consolidation_module
from advanced_omi_backend.services.timeline.consolidation import (
    _validated_suggestions,
    prefetch_consolidation_horizon,
    queue_day_consolidation,
    render_day_tape_png,
    resolve_day_consolidation,
    suggest_episode_consolidation,
)
from advanced_omi_backend.workers import timeline_jobs

BASE = datetime(2026, 2, 15, tzinfo=timezone.utc)


def episode(index: int, minute: int):
    return SimpleNamespace(
        episode_id=f"episode-{index}",
        started_at=BASE + timedelta(minutes=minute),
        ended_at=BASE + timedelta(minutes=minute + 10),
        activity_mode="foreground",
        conversational=False,
        title=f"Episode {index}",
    )


def test_day_tape_is_a_real_png_with_one_row_per_episode():
    image = render_day_tape_png([episode(1, 10), episode(2, 40)], "Asia/Kolkata")

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 500


def test_suggestions_map_labels_to_ids_and_reject_overlap_or_non_contiguous_groups():
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

    result = _validated_suggestions(raw, episodes)

    assert len(result) == 1
    assert result[0].episode_ids == ["episode-1", "episode-2"]
    assert result[0].title == "One task"


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
        await suggest_episode_consolidation(episodes, {}, "Asia/Kolkata")

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

    result = await suggest_episode_consolidation(episodes, {}, "Asia/Kolkata")

    assert result["suggestions"] == []
    assert create.await_count == 1


async def test_resolution_persists_groups_and_accept_reject_training_decisions(
    monkeypatch,
):
    episodes = [episode(1, 10), episode(2, 40), episode(3, 70), episode(4, 100)]
    for index, item in enumerate(episodes, 1):
        item.episode_key = f"key-{index}"
    recorded = {}

    class Cursor:
        def sort(self, *_args):
            return self

        async def to_list(self):
            return episodes

    class EpisodeModel:
        user_id = "user_id"
        run_id = "run_id"

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
        return SimpleNamespace(title="Combined task", summary="One coherent task.")

    monkeypatch.setattr(consolidation_module, "TimelineEpisode", EpisodeModel)
    monkeypatch.setattr(consolidation_module, "TimelineDay", DayModel)
    monkeypatch.setattr(
        consolidation_module, "synthesize_merged_episode_account", synthesize
    )
    day = SimpleNamespace(
        id="day-one",
        user_id="user-one",
        active_run_id="run-one",
        consolidation_state="ready",
        consolidation_model="qwen",
        consolidation_suggestions=[
            {
                "suggestion_id": "group:first",
                "episode_ids": ["episode-1", "episode-2"],
                "title": "First task",
                "reason": "Same task",
                "confidence": 0.9,
            },
            {
                "suggestion_id": "group:second",
                "episode_ids": ["episode-3", "episode-4"],
                "title": "Second task",
                "reason": "Possibly related",
                "confidence": 0.7,
            },
        ],
        semantic_groups=[],
    )

    groups = await resolve_day_consolidation(day, ["group:first"])

    assert len(groups) == 1
    assert groups[0].episode_ids == ["episode-1", "episode-2"]
    assert recorded["update"]["$set"]["consolidation_state"] == "resolved"
    assert len(recorded["update"]["$push"]["semantic_groups"]["$each"]) == 1
    decisions = recorded["update"]["$push"]["review_decisions"]["$each"]
    assert [item["action"] for item in decisions] == [
        "grouping_accept",
        "grouping_reject",
    ]
    assert all(item["before"]["suggestion_id"] for item in decisions)


async def test_pregeneration_job_calls_the_run_fenced_generator(monkeypatch):
    observed = {}

    async def fake_generate(user_id, local_date, timezone_name, run_id):
        observed.update(
            user_id=user_id,
            local_date=local_date,
            timezone=timezone_name,
            run_id=run_id,
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
        "run_id": "run-1",
    }


async def test_manual_grouping_request_enqueues_the_registered_worker(monkeypatch):
    queue = SimpleNamespace(enqueue=Mock())
    monkeypatch.setattr(
        "advanced_omi_backend.controllers.queue_controller.default_queue", queue
    )
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.executor.settings_dict",
        lambda: {"consolidation": {"timeout_seconds": 345}},
    )

    result = await queue_day_consolidation(
        "user-1", date(2026, 2, 19), "Asia/Kolkata", "run-1"
    )

    assert result == {
        "state": "queued",
        "run_id": "run-1",
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
            "active_run_id": f"run-{index}",
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
        "advanced_omi_backend.services.timeline.executor.settings_dict",
        lambda: {"consolidation": {"pregenerate": True, "prefetch_days": 5}},
    )
    monkeypatch.setattr(
        "advanced_omi_backend.controllers.queue_controller.default_queue", queue
    )
    monkeypatch.setattr(
        "advanced_omi_backend.models.timeline.TimelineDay.get_pymongo_collection",
        lambda: collection,
    )

    result = await prefetch_consolidation_horizon()

    assert result == {"users": 1, "considered": 5, "queued": 4, "failed": 0}
    assert queue.enqueue.call_count == 4
