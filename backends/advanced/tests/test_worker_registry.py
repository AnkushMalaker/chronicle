from advanced_omi_backend.workers.orchestrator.worker_registry import (
    build_worker_definitions,
)


def test_every_post_conversation_queue_has_a_worker():
    served_queues = {
        queue for worker in build_worker_definitions() for queue in worker.queues
    }

    assert {"transcription", "memory", "summary", "default"} <= served_queues
