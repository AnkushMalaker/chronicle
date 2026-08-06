"""Authenticated semantic timeline APIs."""

from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.timeline.discovery import (
    process_timeline_analysis_runs,
    request_timeline_analysis,
)
from advanced_omi_backend.services.timeline.timezone import canonical_timezone

router = APIRouter(prefix="/timeline", tags=["timeline"])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_timezone(value: str) -> str:
    try:
        return canonical_timezone(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown IANA timezone") from error


def _run_payload(run: TimelineAnalysisRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "state": run.state,
        "attempts": run.attempts,
        "retry_after": _utc(run.retry_after),
        "error": run.error,
        "requested_evidence_revision": run.evidence_revision,
        "processed_evidence_revision": run.processed_evidence_revision,
        "created_at": _utc(run.created_at),
        "completed_at": _utc(run.completed_at),
    }


def _episode_payload(episode: TimelineEpisode) -> dict:
    return {
        "episode_id": episode.episode_id,
        "started_at": _utc(episode.started_at),
        "ended_at": _utc(episode.ended_at),
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "status": episode.status,
        "salience": episode.salience,
        "confidence": episode.confidence,
        "activity_mode": episode.activity_mode,
        "entities": episode.entities,
        "attributes": episode.attributes,
        "assertions": [
            assertion.model_dump(mode="json") for assertion in episode.assertions
        ],
        "evidence": [
            ref.model_copy(
                update={
                    "started_at": _utc(ref.started_at),
                    "ended_at": _utc(ref.ended_at),
                }
            ).model_dump(mode="json")
            for ref in episode.evidence_refs
        ],
        "related_episode_ids": episode.related_episode_ids,
        "related_conversation_ids": episode.related_conversation_ids,
        "parent_episode_id": episode.parent_episode_id,
        "has_thumbnail": bool(episode.representative_image),
    }


@router.get("/day")
async def get_timeline_day(
    local_date: date = Query(alias="date"),
    timezone: str = Query(),
    user: User = Depends(current_active_user),
):
    timezone = _validate_timezone(timezone)
    owner = str(user.id)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == owner,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone,
    )
    episodes: list[TimelineEpisode] = []
    if day and day.active_run_id:
        episodes = (
            await TimelineEpisode.find(
                TimelineEpisode.user_id == owner,
                TimelineEpisode.run_id == day.active_run_id,
            )
            .sort("started_at")
            .to_list()
        )
    latest = (
        await TimelineAnalysisRun.find(
            TimelineAnalysisRun.user_id == owner,
            TimelineAnalysisRun.local_date == local_date,
            TimelineAnalysisRun.timezone == timezone,
        )
        .sort("-created_at")
        .first_or_none()
    )
    return {
        "date": local_date,
        "timezone": timezone,
        "active_run_id": day.active_run_id if day else None,
        "coverage": day.coverage if day else {},
        "analysis": _run_payload(latest),
        "episodes": [_episode_payload(episode) for episode in episodes],
    }


class AnalyzeRequest(BaseModel):
    date: date
    timezone: str
    force: bool = False


@router.post("/analyze")
async def analyze_timeline_day(
    body: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    timezone = _validate_timezone(body.timezone)
    run = await request_timeline_analysis(
        str(user.id), body.date, timezone, force=body.force
    )
    # Manual requests start promptly without holding the HTTP connection; the shared
    # compare-and-set claim prevents the cron and this task from owning it together.
    background_tasks.add_task(process_timeline_analysis_runs, 1)
    return _run_payload(run)


@router.get("/analysis/{run_id}")
async def get_timeline_analysis(run_id: str, user: User = Depends(current_active_user)):
    run = await TimelineAnalysisRun.find_one(
        TimelineAnalysisRun.run_id == run_id,
        TimelineAnalysisRun.user_id == str(user.id),
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Timeline analysis not found")
    return _run_payload(run)


@router.get("/episodes/{episode_id}/thumbnail")
async def get_episode_thumbnail(
    episode_id: str, user: User = Depends(current_active_user)
):
    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == str(user.id),
    )
    if episode is None or not episode.representative_image:
        raise HTTPException(status_code=404, detail="Episode thumbnail not found")
    return Response(
        content=episode.representative_image,
        media_type=episode.representative_image_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


class TimezoneRequest(BaseModel):
    timezone: str


@router.put("/timezone")
async def update_timeline_timezone(
    body: TimezoneRequest, user: User = Depends(current_active_user)
):
    user.timezone = _validate_timezone(body.timezone)
    await user.save()
    return {"timezone": user.timezone}
