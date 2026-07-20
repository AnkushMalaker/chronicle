"""Single-flight guarantee for the per-session audio-persistence job.

A WebSocket reconnect mid-session re-runs ``start_streaming_jobs``. The audio
persistence job MUST be single-flight per session: re-enqueuing while one is
already live (i.e. a reconnect) must reuse the live job, never start a second
consumer.

Why this matters (the bug this guards against): two persistence jobs for one
session share the SAME Redis consumer name (``persistence-{session_id[:8]}``),
so the audio stream gets SPLIT between them — each new message goes to only one
consumer. The speech-detected conversations created after the reconnect then
find no audio chunks under their id and get deleted as ``audio_chunks_not_ready``
while their transcripts are stranded on a different conversation document.
"""

import pytest
from fakeredis import FakeStrictRedis
from rq import Queue

pytestmark = pytest.mark.unit


@pytest.fixture
def qc(monkeypatch):
    """queue_controller with its Redis + audio queue pointed at fakeredis."""
    fake = FakeStrictRedis()
    from advanced_omi_backend.controllers import queue_controller as module

    monkeypatch.setattr(module, "redis_conn", fake)
    monkeypatch.setattr(module, "audio_queue", Queue("audio", connection=fake))
    return module


def test_reconnect_reuses_live_persistence_job(qc):
    """A second enqueue for the same session (a reconnect) reuses the live job."""
    first = qc.enqueue_audio_persistence(
        "sess-1", "user-1", "sess-1", always_persist=True
    )
    second = qc.enqueue_audio_persistence(
        "sess-1", "user-1", "sess-1", always_persist=True
    )

    assert first == second, "reconnect must reuse the live persistence job id"
    assert qc.audio_queue.count == 1, "must not enqueue a second persistence consumer"


def test_distinct_sessions_get_distinct_jobs(qc):
    """Single-flight is per-session: different sessions each get their own job."""
    a = qc.enqueue_audio_persistence("sess-A", "user-1", "sess-A", always_persist=True)
    b = qc.enqueue_audio_persistence("sess-B", "user-1", "sess-B", always_persist=True)

    assert a != b
    assert qc.audio_queue.count == 2


def test_ended_job_allows_a_fresh_enqueue(qc):
    """After the persistence job terminates, a new session may enqueue again.

    Single-flight must gate on LIVENESS, not merely on the id ever having existed —
    otherwise a clean session end would permanently block the next session.
    """
    from rq.job import Job, JobStatus

    first = qc.enqueue_audio_persistence(
        "sess-1", "user-1", "sess-1", always_persist=True
    )
    # Simulate the job terminating (worker finished/abandoned it).
    job = Job.fetch(first, connection=qc.redis_conn)
    job.set_status(JobStatus.FINISHED)

    second = qc.enqueue_audio_persistence(
        "sess-1", "user-1", "sess-1", always_persist=True
    )
    assert qc._job_is_live(
        second
    ), "a fresh persistence job must be live after re-enqueue"
