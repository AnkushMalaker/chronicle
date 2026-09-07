from types import SimpleNamespace

from backend.controllers import queue_controller
from backend.models.conversation import Conversation


class RecordingQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, _function, *args, **kwargs):
        job = SimpleNamespace(
            id=kwargs["job_id"],
            meta=kwargs.get("meta", {}),
            kwargs=kwargs,
            args=args,
        )
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
        "title": None,
        "short_summary": None,
        "detailed_summary": None,
        "event_dispatch": "event_complete_screenpipe-c",
    }
    assert [job.id for job in default_queue.jobs] == ["event_complete_screenpipe-c"]
    assert events[0][2]["jobs"] == ["event_dispatch"]


def test_memory_spaces_and_main_timeline_both_wait_for_review(monkeypatch):
    default_queue = RecordingQueue()
    memory_queue = RecordingQueue()

    monkeypatch.setattr(queue_controller, "default_queue", default_queue)
    monkeypatch.setattr(queue_controller, "memory_queue", memory_queue)
    monkeypatch.setattr(
        queue_controller, "_clear_post_conversation_chain", lambda _id: []
    )
    monkeypatch.setattr(
        queue_controller,
        "get_service_config",
        lambda service: {
            "enabled": service == "memory.extraction",
        },
    )
    monkeypatch.setattr(
        queue_controller,
        "post_conv_enqueue_kwargs",
        lambda stage, meta, depends_on=None: {
            "meta": {**meta, "failure_stage": stage},
            "depends_on": depends_on,
        },
    )
    monkeypatch.setattr(queue_controller, "publish_sse_event", lambda *args: None)

    space_jobs = queue_controller.start_post_conversation_jobs(
        conversation_id="space-conversation",
        user_id="user-1",
        memory_space_id="space-1",
        skip_title_summary=True,
    )
    main_jobs = queue_controller.start_post_conversation_jobs(
        conversation_id="main-conversation",
        user_id="user-1",
        skip_title_summary=True,
    )

    assert space_jobs["memory"] is None
    assert main_jobs["memory"] is None
    assert memory_queue.jobs == []


def test_post_conversation_summary_bundle_has_independent_execution_timeouts(
    monkeypatch,
):
    default_queue = RecordingQueue()
    summary_queue = RecordingQueue()

    monkeypatch.setattr(queue_controller, "default_queue", default_queue)
    monkeypatch.setattr(queue_controller, "summary_queue", summary_queue)
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
        "post_conv_enqueue_kwargs",
        lambda stage, meta, depends_on=None: {
            "meta": {**meta, "failure_stage": stage},
            "depends_on": depends_on,
        },
    )
    monkeypatch.setattr(queue_controller, "publish_sse_event", lambda *args: None)

    queue_controller.start_post_conversation_jobs(
        conversation_id="local-title-model",
        user_id="user-1",
        skip_memory_extraction=True,
        memory_space_id="space-1",
    )

    title_job = next(
        job for job in summary_queue.jobs if job.id == "title_local-title-"
    )
    short_job = next(
        job for job in summary_queue.jobs if job.id == "short_summary_local-title-"
    )
    detailed_job = next(
        job for job in summary_queue.jobs if job.id == "detailed_summary_local-title-"
    )

    assert title_job.kwargs["job_timeout"] == 30
    assert short_job.kwargs["job_timeout"] == 60
    assert detailed_job.kwargs["job_timeout"] == 300
    assert short_job.kwargs["depends_on"] is title_job
    assert detailed_job.kwargs["depends_on"] is short_job

    event_job = next(
        job for job in default_queue.jobs if job.id == "event_complete_local-title-"
    )
    assert event_job.kwargs["depends_on"] == [detailed_job]


def test_a_processing_trigger_is_not_stored_as_an_end_reason(monkeypatch):
    """Why the pipeline ran and why the recording ended are different questions.

    ``file_upload``, ``reprocess_orphan``, ``reprocess_transcript`` and ``rebound``
    were all passed as ``end_reason`` while none of them is a ``Conversation.EndReason``
    member, so each was silently stored as ``UNKNOWN`` while the emitted event still
    reported the raw string. They travel in ``trigger`` now, and a reprocess passes no
    end reason at all so the recording keeps the one it really ended with.
    """

    default_queue = RecordingQueue()

    monkeypatch.setattr(queue_controller, "default_queue", default_queue)
    monkeypatch.setattr(
        queue_controller, "_clear_post_conversation_chain", lambda _id: []
    )
    monkeypatch.setattr(
        queue_controller, "get_service_config", lambda _service: {"enabled": False}
    )
    monkeypatch.setattr(queue_controller, "publish_sse_event", lambda *_a, **_k: None)

    queue_controller.start_post_conversation_jobs(
        conversation_id="reprocessed-conversation",
        user_id="user-1",
        trigger=Conversation.ProcessingTrigger.REPROCESS_TRANSCRIPT.value,
        skip_memory_extraction=True,
        skip_title_summary=True,
        skip_speaker_recognition=True,
    )

    dispatch = default_queue.jobs[-1]
    # enqueue(fn, conversation_id, client_id, user_id, end_reason, trigger)
    conversation_id, _client_id, _user_id, end_reason, trigger = dispatch.args
    assert conversation_id == "reprocessed-conversation"
    assert end_reason is None
    assert trigger == "reprocess_transcript"


def test_every_processing_trigger_is_distinct_from_every_end_reason():
    """The two vocabularies must not share a value, or the split is cosmetic."""

    end_reasons = {member.value for member in Conversation.EndReason}
    triggers = {member.value for member in Conversation.ProcessingTrigger}

    assert end_reasons.isdisjoint(triggers), end_reasons & triggers
    # The four values the audit found being passed as end reasons all live here now.
    assert {
        "file_upload",
        "reprocess_orphan",
        "reprocess_transcript",
        "rebound",
    } <= triggers


def test_silence_trim_is_a_derived_operation():
    """``maybe_trim_silence`` passes this; it raised at DerivedFrom construction."""

    assert Conversation.DerivedOperation("silence_trim")
    assert Conversation.DerivedOperation("rebound")
