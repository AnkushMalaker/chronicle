from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.models.timeline import EvidenceLocator
from backend.routers.modules import timeline_routes
from backend.services.timeline.contracts import (
    EvidenceBundle,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)

START = datetime(2026, 8, 19, 4, 30, tzinfo=timezone.utc)
TEXT = (
    "We should preserve immutable audio chunks and align every source clock "
    "before grouping the evidence into one semantic activity"
)


def _bundle() -> EvidenceBundle:
    evidence = [
        TimelineEvidenceItem(
            evidence_id=f"transcript:{source_id}",
            kind="transcript",
            source_id=source_id,
            locator=EvidenceLocator(
                capture_source_id=source_id,
                modality="transcript",
                track_id="input",
            ),
            started_at=START,
            ended_at=START + timedelta(minutes=20),
            role="uncertain",
            excerpt=TEXT,
            metadata={"direction": "input"},
        )
        for source_id in ("wearable", "laptop")
    ]
    return EvidenceBundle(
        manifest=TimelineEvidenceManifest(
            user_id="owner-one",
            local_date=date(2026, 8, 19),
            timezone="Asia/Kolkata",
            started_at=START,
            ended_at=START + timedelta(minutes=20),
            evidence_revision="manifest-hash",
            windows=[],
            evidence=evidence,
        ),
        evidence_revision=7,
    )


@pytest.mark.asyncio
async def test_preview_is_bounded_user_scoped_and_read_only(monkeypatch):
    calls = []

    async def load(user_id, started_at, ended_at, *, timezone_name):
        calls.append((user_id, started_at, ended_at, timezone_name))
        return _bundle()

    monkeypatch.setattr(timeline_routes, "load_reconciliation_evidence", load)

    preview = await timeline_routes.preview_evidence_relations(
        START,
        START + timedelta(minutes=20),
        "Asia/Kolkata",
        user=SimpleNamespace(id="owner-one"),
    )

    assert calls == [
        (
            "owner-one",
            START,
            START + timedelta(minutes=20),
            "Asia/Kolkata",
        )
    ]
    assert preview.evidence_revision == "manifest-hash"
    assert preview.relation_count == 1
    assert preview.relations[0].relation_type == "corroborates"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "started_at,ended_at",
    [
        (START, START),
        (START + timedelta(minutes=1), START),
        (START, START + timedelta(hours=7)),
    ],
)
async def test_preview_rejects_impossible_ranges(started_at, ended_at):
    with pytest.raises(HTTPException) as error:
        await timeline_routes.preview_evidence_relations(
            started_at,
            ended_at,
            "Etc/UTC",
            user=SimpleNamespace(id="owner-one"),
        )

    assert error.value.status_code == 422
