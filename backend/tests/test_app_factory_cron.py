from pathlib import Path

from backend import app_factory, cron_scheduler
from backend.config_loader import load_config


def test_production_cron_registration_keeps_timeline_explicit(monkeypatch):
    registered = {}
    monkeypatch.setattr(
        app_factory,
        "register_cron_job",
        lambda name, entrypoint: registered.setdefault(name, entrypoint),
    )

    app_factory.register_application_cron_jobs()

    assert registered["notification_dispatch"] is app_factory.queue_due_notifications
    assert registered["notification_receipts"] is app_factory.queue_receipt_check
    assert (
        registered["rolling_reconciliation_scan"] is app_factory.reconcile_dirty_ranges
    )
    assert (
        registered["timeline_publication_recovery"]
        is app_factory.recover_timeline_publications
    )
    assert (
        registered["timeline_episode_dispatch_recovery"]
        is app_factory.dispatch_ready_episodes
    )
    assert "timeline_analysis" not in registered
    assert "immich_memories" not in registered


def test_effective_scheduler_enables_timeline_publication_recovery(monkeypatch):
    config_dir = Path(__file__).parents[2] / "config"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CONFIG_FILE", "missing-test-overrides.yml")
    monkeypatch.setattr(
        cron_scheduler,
        "load_config",
        lambda: load_config(force_reload=True),
    )
    scheduler = cron_scheduler.CronScheduler()

    scheduler._load_jobs_from_config()

    recovery = scheduler.jobs["timeline_publication_recovery"]
    assert recovery.enabled is True
    assert recovery.schedule == "*/5 * * * *"


def test_effective_scheduler_enables_timeline_episode_dispatch_recovery(monkeypatch):
    config_dir = Path(__file__).parents[2] / "config"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CONFIG_FILE", "missing-test-overrides.yml")
    monkeypatch.setattr(
        cron_scheduler,
        "load_config",
        lambda: load_config(force_reload=True),
    )
    scheduler = cron_scheduler.CronScheduler()

    scheduler._load_jobs_from_config()

    recovery = scheduler.jobs["timeline_episode_dispatch_recovery"]
    assert recovery.enabled is True
    assert recovery.schedule == "*/5 * * * *"
