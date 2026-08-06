from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.timeline import discovery
from advanced_omi_backend.services.timeline.contracts import TimelineEvidenceManifest


@pytest.mark.asyncio
async def test_processing_records_the_manifest_revision_actually_used(monkeypatch):
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        evidence_revision="processed-revision",
        windows=[],
        evidence=[],
    )

    async def assemble(*args, **kwargs):
        return manifest, {}

    monkeypatch.setattr(discovery, "assemble_day_evidence", assemble)
    monkeypatch.setattr(discovery, "settings_dict", lambda: {})

    class Run(SimpleNamespace):
        async def save(self):
            return self

    run = Run(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        processed_evidence_revision=None,
        coverage_window_ids=[],
        state="preparing",
        completed_at=None,
    )

    await discovery._process_run(run)

    assert run.processed_evidence_revision == "processed-revision"
    assert run.state == "awaiting_evidence"
