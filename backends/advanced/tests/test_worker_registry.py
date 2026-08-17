import yaml

from advanced_omi_backend.workers.orchestrator import worker_registry
from advanced_omi_backend.workers.orchestrator.worker_registry import (
    build_worker_definitions,
)


def test_every_post_conversation_queue_has_a_worker():
    served_queues = {
        queue for worker in build_worker_definitions() for queue in worker.queues
    }

    assert {"transcription", "memory", "summary", "default"} <= served_queues


def test_interaction_mode_also_enables_acoustic_wake_dispatch(tmp_path, monkeypatch):
    plugins_path = tmp_path / "plugins.yml"
    plugins_path.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "swiggy_instamart": {
                        "enabled": True,
                        "events": [],
                        "modes": ["swiggy_order"],
                    }
                }
            }
        )
    )
    monkeypatch.setattr(worker_registry, "get_plugins_yml_path", lambda: plugins_path)

    assert worker_registry.has_interaction_modes_enabled()
    assert worker_registry.has_wakeword_dispatch_enabled()
