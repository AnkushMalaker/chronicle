"""Idempotent timeline run coordination and generation publishing."""

import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineAssertion,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    utcnow,
)
from advanced_omi_backend.models.user import User

from .codex_executor import TimelineQuotaDeferred
from .contracts import TimelineAgentResult, TimelineEvidenceManifest
from .evidence import assemble_day_evidence, day_bounds
from .executor import build_executor, settings_dict, validate_agent_result
from .workspace import write_workspace


async def request_timeline_analysis(
    user_id: str,
    local_date: date,
    timezone_name: str,
    force: bool = False,
) -> TimelineAnalysisRun:
    settings = settings_dict()
    manifest, _ = await assemble_day_evidence(
        user_id,
        local_date,
        timezone_name,
        window_minutes=int(settings.get("window_minutes", 20)),
        overlap_minutes=int(settings.get("overlap_minutes", 3)),
    )
    revision = manifest.evidence_revision
    if force:
        revision = f"{revision}:force:{uuid.uuid4().hex}"
    prompt_version = str(settings.get("prompt_version") or "timeline-episodes-v1")
    existing = await TimelineAnalysisRun.find_one(
        TimelineAnalysisRun.user_id == user_id,
        TimelineAnalysisRun.local_date == local_date,
        TimelineAnalysisRun.timezone == timezone_name,
        TimelineAnalysisRun.evidence_revision == revision,
        TimelineAnalysisRun.prompt_version == prompt_version,
    )
    if existing is not None:
        return existing
    day_start, day_end = day_bounds(local_date, timezone_name)
    run = TimelineAnalysisRun(
        user_id=user_id,
        local_date=local_date,
        timezone=timezone_name,
        day_started_at=day_start,
        day_ended_at=day_end,
        evidence_revision=revision,
        prompt_version=prompt_version,
        executor=str(settings.get("executor") or "codex"),
    )
    try:
        await run.insert()
        return run
    except DuplicateKeyError:
        existing = await TimelineAnalysisRun.find_one(
            TimelineAnalysisRun.user_id == user_id,
            TimelineAnalysisRun.local_date == local_date,
            TimelineAnalysisRun.timezone == timezone_name,
            TimelineAnalysisRun.evidence_revision == revision,
            TimelineAnalysisRun.prompt_version == prompt_version,
        )
        if existing is None:
            raise
        return existing


async def _claim_next_run() -> TimelineAnalysisRun | None:
    now = utcnow()
    stale = now - timedelta(hours=2)
    collection = TimelineAnalysisRun.get_pymongo_collection()
    document = await collection.find_one_and_update(
        {
            "$or": [
                {"state": "pending"},
                {"state": "quota_deferred", "retry_after": {"$lte": now}},
                {
                    "state": {"$in": ["preparing", "running", "validating"]},
                    "claimed_at": {"$lt": stale},
                },
            ]
        },
        {
            "$set": {"state": "preparing", "claimed_at": now, "error": None},
            "$inc": {"attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return await TimelineAnalysisRun.find_one(
        TimelineAnalysisRun.run_id == document["run_id"]
    )


def _existing_payload(episodes: list[TimelineEpisode]) -> list[dict[str, Any]]:
    return [
        {
            "episode_id": episode.episode_id,
            "started_at": episode.started_at.isoformat(),
            "ended_at": episode.ended_at.isoformat(),
            "kind": episode.kind,
            "title": episode.title,
            "summary": episode.summary,
        }
        for episode in episodes
    ]


async def _active_episodes(run: TimelineAnalysisRun) -> list[TimelineEpisode]:
    day = await TimelineDay.find_one(
        TimelineDay.user_id == run.user_id,
        TimelineDay.local_date == run.local_date,
        TimelineDay.timezone == run.timezone,
    )
    if day is None or not day.active_run_id:
        return []
    return await TimelineEpisode.find(
        TimelineEpisode.run_id == day.active_run_id,
        TimelineEpisode.user_id == run.user_id,
    ).to_list()


def _evidence_ref(item) -> TimelineEvidenceRef:
    return TimelineEvidenceRef(
        evidence_id=item.evidence_id,
        kind=item.kind,
        source_id=item.source_id,
        source_item_id=item.source_item_id,
        started_at=item.started_at,
        ended_at=item.ended_at,
        role=item.role,
        excerpt=item.excerpt,
        content_hash=item.content_hash,
        ephemeral=item.ephemeral,
    )


async def _publish(
    run: TimelineAnalysisRun,
    manifest: TimelineEvidenceManifest,
    result: TimelineAgentResult,
    images: dict[str, bytes],
) -> None:
    evidence = {item.evidence_id: item for item in manifest.evidence}
    episode_ids = [str(uuid.uuid4()) for _ in result.episodes]
    documents: list[TimelineEpisode] = []
    for index, episode in enumerate(result.episodes):
        refs = [
            _evidence_ref(evidence[evidence_id]) for evidence_id in episode.evidence_ids
        ]
        representative = episode.representative_evidence_id
        documents.append(
            TimelineEpisode(
                episode_id=episode_ids[index],
                run_id=run.run_id,
                user_id=run.user_id,
                local_date=run.local_date,
                timezone=run.timezone,
                started_at=episode.started_at,
                ended_at=episode.ended_at,
                kind=episode.kind,
                title=episode.title,
                summary=episode.summary,
                salience=episode.salience,
                confidence=episode.confidence,
                activity_mode=episode.activity_mode,
                entities=episode.entities,
                attributes=episode.attributes,
                assertions=[
                    TimelineAssertion(**assertion.model_dump())
                    for assertion in episode.assertions
                ],
                evidence_refs=refs,
                source_ids=sorted({ref.source_id for ref in refs if ref.source_id}),
                related_conversation_ids=episode.related_conversation_ids,
                parent_episode_id=(
                    episode_ids[episode.parent_episode_index]
                    if episode.parent_episode_index is not None
                    else None
                ),
                representative_image=images.get(representative or ""),
                representative_image_type=(
                    evidence[representative].metadata.get("image_content_type")
                    if representative and representative in images
                    else None
                ),
            )
        )
    if documents:
        await TimelineEpisode.insert_many(documents)
    coverage = {
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat(),
        "window_count": len(manifest.windows),
        "evidence_count": len(manifest.evidence),
        "unassigned_intervals": [
            interval.model_dump(mode="json") for interval in result.unassigned_intervals
        ],
    }
    collection = TimelineDay.get_pymongo_collection()
    encoded_local_date = datetime.combine(run.local_date, time.min)
    identity = {
        "user_id": run.user_id,
        "local_date": encoded_local_date,
        "timezone": run.timezone,
    }
    await collection.update_one(
        identity,
        {
            "$setOnInsert": {
                **identity,
                "active_run_id": None,
                "active_run_created_at": None,
                "evidence_revision": None,
                "coverage": {},
                "revised_at": utcnow(),
            }
        },
        upsert=True,
    )
    await collection.update_one(
        {
            **identity,
            "$or": [
                {"active_run_created_at": {"$lte": run.created_at}},
                {"active_run_created_at": None},
            ],
        },
        {
            "$set": {
                "active_run_id": run.run_id,
                "active_run_created_at": run.created_at,
                "evidence_revision": manifest.evidence_revision,
                "coverage": coverage,
                "revised_at": utcnow(),
            },
        },
    )
    run.output_episode_ids = episode_ids


async def _process_run(run: TimelineAnalysisRun) -> None:
    settings = settings_dict()
    manifest, images = await assemble_day_evidence(
        run.user_id,
        run.local_date,
        run.timezone,
        window_minutes=int(settings.get("window_minutes", 20)),
        overlap_minutes=int(settings.get("overlap_minutes", 3)),
    )
    if not manifest.evidence:
        run.state = "awaiting_evidence"
        run.coverage_window_ids = [window.window_id for window in manifest.windows]
        run.completed_at = utcnow()
        await run.save()
        return
    run.coverage_window_ids = [window.window_id for window in manifest.windows]
    run.state = "running"
    await run.save()
    existing = await _active_episodes(run)
    with tempfile.TemporaryDirectory(prefix="chronicle-timeline-") as temp_dir:
        workspace = Path(temp_dir)
        write_workspace(
            workspace,
            manifest,
            images,
            max_text_chars_per_window=int(
                settings.get("max_text_chars_per_window", 30000)
            ),
            max_anchor_images_per_window=int(
                settings.get("max_anchor_images_per_window", 4)
            ),
        )
        result = await build_executor().analyze(
            workspace, manifest, _existing_payload(existing)
        )
    run.state = "validating"
    await run.save()
    validate_agent_result(result, manifest)
    await _publish(run, manifest, result, images)
    run.state = "complete"
    run.completed_at = utcnow()
    await run.save()


async def process_timeline_analysis_runs(max_runs: int = 1) -> dict[str, int]:
    processed = failed = deferred = 0
    settings = settings_dict()
    retry_hours = int((settings.get("codex") or {}).get("retry_hours", 6))
    for _ in range(max_runs):
        run = await _claim_next_run()
        if run is None:
            break
        try:
            await _process_run(run)
            processed += 1
        except TimelineQuotaDeferred as error:
            run.state = "quota_deferred"
            run.retry_after = utcnow() + timedelta(hours=retry_hours)
            run.error = str(error)[:2000]
            run.usage = error.usage
            await run.save()
            deferred += 1
        except Exception as error:
            run.state = "failed"
            run.error = f"{type(error).__name__}: {error}"[:4000]
            run.completed_at = utcnow()
            await run.save()
            failed += 1
    return {"processed": processed, "failed": failed, "deferred": deferred}


async def process_current_timeline_days() -> dict[str, int]:
    requested = 0
    users = await User.find({"timezone": {"$nin": [None, ""]}}).to_list()
    now = datetime.now(timezone.utc)
    for user in users:
        zone = ZoneInfo(user.timezone)
        today = now.astimezone(zone).date()
        # Current day gives prompt updates; previous day gets one final reconciliation
        # on the first scheduler tick after midnight, then evidence dedupe makes it free.
        for local_date in (today, today - timedelta(days=1)):
            await request_timeline_analysis(str(user.id), local_date, user.timezone)
            requested += 1
    result = await process_timeline_analysis_runs(max_runs=max(1, len(users)))
    return {"requested": requested, **result}
