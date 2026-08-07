from types import SimpleNamespace

from advanced_omi_backend.controllers import queue_controller


class RecordingQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, _function, *_args, **kwargs):
        job = SimpleNamespace(id=kwargs["job_id"], meta=kwargs.get("meta", {}))
        self.jobs.append(job)
        return job


def test_post_conversation_jobs_can_skip_optional_screenpipe_work(monkeypatch):
    default_queue = RecordingQueue()
    events = []

    monkeypatch.setattr(queue_controller, "default_queue", default_queue)
    monkeypatch.setattr(
        queue_controller, "_clear_post_conversation_chain", lambda _id: []
    )
    monkeypatch.setattr(
        queue_controller,
        "get_service_config",
        lambda _service: {"enabled": False},
    )
    monkeypatch.setattr(
        queue_controller,
        "publish_sse_event",
        lambda user_id, event, payload: events.append((user_id, event, payload)),
    )

    jobs = queue_controller.start_post_conversation_jobs(
        conversation_id="screenpipe-conversation",
        user_id="user-1",
        skip_memory_extraction=True,
        skip_title_summary=True,
    )

    assert jobs == {
        "speaker_recognition": None,
        "memory": None,
        "title_summary": None,
        "event_dispatch": "event_complete_screenpipe-c",
    }
    assert [job.id for job in default_queue.jobs] == ["event_complete_screenpipe-c"]
    assert events[0][2]["jobs"] == ["event_dispatch"]
