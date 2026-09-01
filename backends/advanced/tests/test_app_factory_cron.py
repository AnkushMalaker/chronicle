from advanced_omi_backend import app_factory


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
    assert "timeline_analysis" not in registered
    assert "immich_memories" not in registered
