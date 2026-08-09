"""Authenticated semantic timeline APIs."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
    utcnow,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.timeline.discovery import (
    process_timeline_run,
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


def _at(value: datetime) -> datetime:
    """UTC-normalize a timestamp that is known to be present."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
        "episode_key": episode.episode_key,
        "started_at": _utc(episode.started_at),
        "ended_at": _utc(episode.ended_at),
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "status": episode.status,
        "confirmed_at": _utc(episode.confirmed_at),
        "confirmed_fields": episode.confirmed_fields,
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
        "audio_ranges": [item.model_dump(mode="json") for item in episode.audio_ranges],
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
    # Targeted at this run, not the shared oldest-first claim: a manual "Analyze day"
    # must analyze the day that was asked for, not spend its work on a backlogged one.
    # The compare-and-set claim still prevents the cron from owning it concurrently.
    background_tasks.add_task(process_timeline_run, run.run_id)
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


async def _owned_episode(episode_id: str, user: User) -> TimelineEpisode:
    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == str(user.id),
    )
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


async def _episode_audio_recordings(episode: TimelineEpisode) -> list[str]:
    """Live recordings whose audio overlaps the episode, in wall-clock order.

    Not the same question as which recordings the episode *cites*: the agent cites
    the evidence it reasoned over, so a 37-minute call can be represented by the one
    recording that carried its most quotable stretch. Playing the episode needs
    whatever audio covers the span, so this is derived from the interval instead.
    """
    # Compatibility/display metadata only. Playback truth is ``audio_ranges``; map
    # their current owners for the existing recording-detail links.
    chunk_ids = [
        chunk_id for item in episode.audio_ranges for chunk_id in item.chunk_ids
    ]
    if not chunk_ids:
        return []
    owners = await AudioChunkDocument.get_pymongo_collection().distinct(
        "conversation_id", {"_id": {"$in": [ObjectId(item) for item in chunk_ids]}}
    )
    return sorted(str(item) for item in owners)


async def _episode_playback_ranges(episode: TimelineEpisode) -> list[dict]:
    """Resolve immutable episode ranges onto chunks' current playback coordinates."""
    playback: list[dict] = []
    for audio_range in episode.audio_ranges:
        chunks = await AudioChunkDocument.find(
            {"_id": {"$in": [ObjectId(item) for item in audio_range.chunk_ids]}}
        ).to_list()
        chunks.sort(key=lambda item: _at(item.captured_at))
        current: dict | None = None
        for chunk in chunks:
            if chunk.captured_at is None:
                continue
            captured = _at(chunk.captured_at)
            absolute_start = max(_at(audio_range.started_at), captured)
            absolute_end = min(
                _at(audio_range.ended_at),
                captured + timedelta(seconds=chunk.duration),
            )
            if absolute_end <= absolute_start:
                continue
            relative_start = (
                chunk.start_time + (absolute_start - captured).total_seconds()
            )
            relative_end = chunk.start_time + (absolute_end - captured).total_seconds()
            if (
                current is not None
                and current["conversation_id"] == chunk.conversation_id
                and abs(current["end"] - relative_start) <= 0.25
            ):
                current["end"] = relative_end
                current["ended_at"] = absolute_end
            else:
                current = {
                    "range_id": audio_range.range_id,
                    "conversation_id": chunk.conversation_id,
                    "start": relative_start,
                    "end": relative_end,
                    "started_at": absolute_start,
                    "ended_at": absolute_end,
                }
                playback.append(current)
    playback.sort(key=lambda item: item["started_at"])
    return playback


@router.get("/episodes/{episode_id}")
async def get_timeline_episode(
    episode_id: str, user: User = Depends(current_active_user)
):
    episode = await _owned_episode(episode_id, user)
    payload = _episode_payload(episode)
    payload["audio_recording_ids"] = await _episode_audio_recordings(episode)
    payload["audio_playback_ranges"] = await _episode_playback_ranges(episode)
    return payload


class EpisodeUpdate(BaseModel):
    """Human corrections to one episode.

    Any field supplied here becomes human-owned: the episode is confirmed, and later
    analysis runs carry it forward instead of regenerating its interval.
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    summary: Optional[str] = Field(default=None, max_length=1200)
    kind: Optional[str] = Field(default=None, min_length=1, max_length=80)
    entities: Optional[list[str]] = None
    salience: Optional[Literal["background", "routine", "notable", "highlight"]] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


@router.patch("/episodes/{episode_id}")
async def update_timeline_episode(
    episode_id: str,
    body: EpisodeUpdate,
    user: User = Depends(current_active_user),
):
    episode = await _owned_episode(episode_id, user)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No episode fields supplied")
    for field, value in changes.items():
        setattr(episode, field, value)
    if _at(episode.ended_at) <= _at(episode.started_at):
        raise HTTPException(
            status_code=422, detail="Episode must have positive duration"
        )
    _confirm(episode, changes.keys())
    await episode.save()
    return _episode_payload(episode)


def _confirm(episode: TimelineEpisode, fields) -> None:
    episode.status = "confirmed"
    episode.confirmed_at = utcnow()
    episode.confirmed_fields = sorted(set(episode.confirmed_fields) | set(fields))
    episode.revised_at = utcnow()


def _refs_overlapping(episode: TimelineEpisode, start: datetime, end: datetime) -> list:
    """Evidence refs touching [start, end). A ref spanning the cut belongs to both sides."""

    kept = []
    for ref in episode.evidence_refs:
        ref_start = _at(ref.started_at)
        ref_end = _at(ref.ended_at) if ref.ended_at else ref_start
        if ref_start < end and ref_end >= start:
            kept.append(ref)
    return kept


class EpisodeSplit(BaseModel):
    at: datetime


@router.post("/episodes/{episode_id}/split")
async def split_timeline_episode(
    episode_id: str,
    body: EpisodeSplit,
    user: User = Depends(current_active_user),
):
    """Cut one episode into two at a timestamp, both confirmed.

    Evidence is repartitioned by overlap rather than copied wholesale, so each half
    only cites what it actually covers. Assertions are filtered to the evidence that
    survives on their side; the model rejects an assertion citing absent evidence.
    """

    episode = await _owned_episode(episode_id, user)
    at = _at(body.at)
    if not (_at(episode.started_at) < at < _at(episode.ended_at)):
        raise HTTPException(
            status_code=422, detail="Split point must fall strictly inside the episode"
        )

    tail = episode.model_copy(deep=True)
    tail.id = None
    tail.episode_id = str(uuid.uuid4())
    # A new durable identity: the tail is a new event, not a continuation of the
    # original's confirmed history.
    tail.episode_key = str(uuid.uuid4())
    tail.started_at = at
    tail.evidence_refs = _refs_overlapping(episode, at, _at(episode.ended_at))
    episode.ended_at = at
    episode.evidence_refs = _refs_overlapping(episode, _at(episode.started_at), at)

    for part in (episode, tail):
        known = {ref.evidence_id for ref in part.evidence_refs}
        part.assertions = [
            assertion
            for assertion in part.assertions
            if set(assertion.evidence_ids) <= known
        ]
        _confirm(part, ["started_at", "ended_at"])

    await tail.insert()
    await episode.save()
    return {
        "episodes": [_episode_payload(episode), _episode_payload(tail)],
    }


class EpisodeMerge(BaseModel):
    episode_ids: list[str] = Field(min_length=2)


@router.post("/episodes/merge")
async def merge_timeline_episodes(
    body: EpisodeMerge, user: User = Depends(current_active_user)
):
    """Collapse several episodes into one spanning their full extent.

    The earliest episode survives and keeps its ``episode_key``; the rest are deleted.
    Evidence, entities, and assertions are unioned.
    """

    episodes = [
        await _owned_episode(episode_id, user) for episode_id in body.episode_ids
    ]
    if len({episode.run_id for episode in episodes}) != 1:
        raise HTTPException(
            status_code=422, detail="Episodes must belong to the same analysis run"
        )
    episodes.sort(key=lambda episode: _at(episode.started_at))
    survivor, absorbed = episodes[0], episodes[1:]

    survivor.ended_at = max(_at(episode.ended_at) for episode in episodes)
    seen = {ref.evidence_id for ref in survivor.evidence_refs}
    for episode in absorbed:
        for ref in episode.evidence_refs:
            if ref.evidence_id not in seen:
                seen.add(ref.evidence_id)
                survivor.evidence_refs.append(ref)
        survivor.entities = sorted(set(survivor.entities) | set(episode.entities))
        survivor.assertions.extend(
            assertion
            for assertion in episode.assertions
            if set(assertion.evidence_ids) <= seen
        )
        survivor.related_conversation_ids = sorted(
            set(survivor.related_conversation_ids)
            | set(episode.related_conversation_ids)
        )
    _confirm(survivor, ["started_at", "ended_at", "entities"])

    await survivor.save()
    for episode in absorbed:
        await episode.delete()
    return _episode_payload(survivor)


@router.delete("/episodes/{episode_id}", status_code=204)
async def delete_timeline_episode(
    episode_id: str, user: User = Depends(current_active_user)
):
    """Remove an episode from this generation.

    The deletion is not a negative label: nothing records that the interval should stay
    unexplained, so a later analysis run may propose an episode there again.
    """

    await (await _owned_episode(episode_id, user)).delete()
    return Response(status_code=204)


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
