"""Idempotent timeline run coordination and generation publishing."""

import logging
import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    post_conv_enqueue_kwargs,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    TimelineAnalysisRun,
    TimelineAssertion,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    utcnow,
)
from advanced_omi_backend.models.user import User
from advanced_omi_backend.observability.tracing import (
    chronicle_span,
    set_span_attributes,
    set_span_usage,
)
from advanced_omi_backend.workers.conversation_jobs import generate_title_summary_job

from .codex_executor import TimelineQuotaDeferred
from .contracts import TimelineAgentResult, TimelineEvidenceManifest
from .evidence import assemble_day_evidence, day_bounds
from .executor import (
    TimelineIncompleteSegmentation,
    build_executor,
    settings_dict,
    validate_agent_result,
)
from .prompt import PROMPT_VERSION
from .recording_refs import build_audio_ranges, resolve_live_recordings
from .timezone import canonical_timezone
from .workspace import write_workspace

logger = logging.getLogger(__name__)


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
    timezone_name = manifest.timezone
    revision = manifest.evidence_revision
    if force:
        revision = f"{revision}:force:{uuid.uuid4().hex}"
    # Falls back to the prompt module's own version rather than a literal, so an
    # unconfigured deployment still tracks the shipped rules instead of pinning "v1".
    prompt_version = str(settings.get("prompt_version") or PROMPT_VERSION)
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


def _claimable(now: datetime) -> dict[str, Any]:
    stale = now - timedelta(hours=2)
    return {
        "$or": [
            {"state": "pending"},
            {"state": "quota_deferred", "retry_after": {"$lte": now}},
            {
                "state": {"$in": ["preparing", "running", "validating"]},
                "claimed_at": {"$lt": stale},
            },
        ]
    }


async def process_timeline_run(run_id: str) -> dict[str, int]:
    """Analyze one specific run now, regardless of what else is queued.

    On-demand analysis used the shared oldest-first claim, so pressing "Analyze day"
    could spend its work on somebody else's backlogged run and leave the requested day
    untouched — indefinitely, if the backlog kept growing.
    """

    now = utcnow()
    collection = TimelineAnalysisRun.get_pymongo_collection()
    document = await collection.find_one_and_update(
        {"run_id": run_id, **_claimable(now)},
        {
            "$set": {"state": "preparing", "claimed_at": now, "error": None},
            "$inc": {"attempts": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        # Already running or finished — the caller's day is being handled either way.
        return {"processed": 0, "failed": 0, "deferred": 0}
    run = await TimelineAnalysisRun.find_one(TimelineAnalysisRun.run_id == run_id)
    if run is None:
        return {"processed": 0, "failed": 0, "deferred": 0}
    return await _run_claimed(run)


async def _claim_next_run() -> TimelineAnalysisRun | None:
    now = utcnow()
    collection = TimelineAnalysisRun.get_pymongo_collection()
    document = await collection.find_one_and_update(
        _claimable(now),
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


def _pinned_payload(episodes: list[TimelineEpisode]) -> list[dict[str, Any]]:
    return [
        {
            "episode_key": episode.episode_key,
            "started_at": episode.started_at.isoformat(),
            "ended_at": episode.ended_at.isoformat(),
            "kind": episode.kind,
            "title": episode.title,
            "summary": episode.summary,
        }
        for episode in episodes
    ]


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
        metadata=dict(getattr(item, "metadata", {}) or {}),
    )


# Escalation ladder for a segmentation pass that came back empty. Low effort is the
# configured default because it is cheap and usually enough on a short day; it degrades
# badly as a day accumulates windows, which is exactly when the retry matters.
_EFFORT_ESCALATION = {"none": "low", "low": "medium", "medium": "high", "high": "high"}


async def _analyze_with_escalation(
    manifest: TimelineEvidenceManifest,
    workspace: Path,
    existing: list[TimelineEpisode],
    pinned: list[TimelineEpisode],
    *,
    configured_effort: Any,
    span: Any = None,
) -> TimelineAgentResult:
    """Run segmentation, retrying once at higher reasoning effort if it says nothing.

    An empty result is a model failure, not an account of the day (see
    ``TimelineIncompleteSegmentation``). Retrying at the same effort would mostly
    reproduce it, so the one retry escalates instead.
    """

    executor = build_executor()
    effort = str(configured_effort) if configured_effort else "low"
    attempts: list[str] = []
    last_error: Exception | None = None

    for attempt_effort in (effort, _EFFORT_ESCALATION.get(effort, "high")):
        if attempts and attempt_effort == attempts[-1]:
            break  # Already at the top of the ladder; a second identical run is waste.
        attempts.append(attempt_effort)
        result = await executor.analyze(
            workspace,
            manifest,
            _existing_payload(existing),
            _pinned_payload(pinned),
            reasoning_effort=attempt_effort,
        )
        try:
            validate_agent_result(
                result,
                manifest,
                [(episode.started_at, episode.ended_at) for episode in pinned],
            )
        except TimelineIncompleteSegmentation as error:
            last_error = error
            logger.warning(
                "🕳️ Timeline segmentation returned nothing at effort=%s (%d windows); "
                "%s",
                attempt_effort,
                len(manifest.windows),
                "retrying at higher effort" if len(attempts) == 1 else "giving up",
            )
            continue
        set_span_attributes(
            span,
            {
                "chronicle.timeline.effort": attempt_effort,
                "chronicle.timeline.segmentation_attempts": len(attempts),
            },
        )
        return result

    set_span_attributes(
        span,
        {
            "chronicle.timeline.effort": attempts[-1] if attempts else effort,
            "chronicle.timeline.segmentation_attempts": len(attempts),
        },
    )
    raise last_error or TimelineIncompleteSegmentation("segmentation produced nothing")


class TimelineEmptyGeneration(RuntimeError):
    """An empty result would have blanked a day that already had episodes."""


async def _guard_empty_generation(
    run: TimelineAnalysisRun,
    manifest: TimelineEvidenceManifest,
    publishing: int,
) -> None:
    """Refuse to let a zero-episode generation supersede a good one.

    A run that returns no episodes still publishes, and publishing switches
    ``TimelineDay.active_run_id`` — so one flaky segmentation pass silently empties a day
    the user could see a minute earlier. Raising leaves the previous generation active;
    the run is recorded as failed and a later evidence revision produces a fresh attempt.

    An empty result is still allowed when the day had nothing to lose, or when the
    evidence itself shrank (deleted captures), where emptiness is the honest answer.
    """

    if publishing:
        return
    day = await TimelineDay.find_one(
        TimelineDay.user_id == run.user_id,
        TimelineDay.local_date == run.local_date,
        TimelineDay.timezone == run.timezone,
    )
    if day is None or not day.active_run_id:
        return
    previous = await TimelineEpisode.find(
        TimelineEpisode.user_id == run.user_id,
        TimelineEpisode.run_id == day.active_run_id,
    ).count()
    if not previous:
        return
    prior_evidence = int((day.coverage or {}).get("evidence_count") or 0)
    if len(manifest.evidence) < prior_evidence:
        return
    raise TimelineEmptyGeneration(
        f"analysis produced no episodes from {len(manifest.evidence)} evidence items "
        f"while the active generation has {previous}; keeping the previous generation"
    )


def _carry_forward(
    run: TimelineAnalysisRun,
    pinned: list[TimelineEpisode],
    manifest: TimelineEvidenceManifest,
) -> list[TimelineEpisode]:
    """Re-materialize confirmed episodes as rows of the new generation.

    Identity (``episode_key``), human-authored fields, and confirmation survive verbatim.
    Evidence refs are refreshed from the new manifest where the cited evidence still
    exists, so a pinned episode picks up re-assembled evidence without losing citations
    that have since aged out.
    """

    evidence = {item.evidence_id: item for item in manifest.evidence}
    carried: list[TimelineEpisode] = []
    for episode in pinned:
        refs = [
            (
                _evidence_ref(evidence[ref.evidence_id])
                if ref.evidence_id in evidence
                else ref
            )
            for ref in episode.evidence_refs
        ]
        payload = episode.model_dump(exclude={"id", "revision_id"})
        payload.update(
            {
                "episode_id": str(uuid.uuid4()),
                "run_id": run.run_id,
                "evidence_refs": refs,
                "source_ids": sorted({ref.source_id for ref in refs if ref.source_id}),
                # A carried episode never parents a freshly drafted one: the draft's
                # parent indices address only this run's generated episodes.
                "parent_episode_id": None,
                "revised_at": utcnow(),
            }
        )
        carried.append(TimelineEpisode(**payload))
    return carried


def _cited_conversation_ids(episode: TimelineEpisode) -> set[str]:
    """Every conversation this episode points at, agent-named or assembly-recorded.

    ``related_conversation_ids`` is the agent's answer and can omit or invent one;
    ``evidence_refs[].metadata['conversation_id']`` is recorded at assembly time and is
    the reliable half. Take the union — the caller only acts on ids that resolve to a
    real capture-evidence recording, which discards anything invented.
    """

    cited = {str(item) for item in episode.related_conversation_ids if item}
    for ref in episode.evidence_refs:
        conversation_id = ref.metadata.get("conversation_id")
        if conversation_id:
            cited.add(str(conversation_id))
    return cited


async def _promote_conversational_recordings(
    episodes: list[TimelineEpisode],
) -> list[str]:
    """Return capture-evidence recordings to the user-facing Recordings list.

    ScreenPipe audio is ingested as ``capture_evidence`` because most of it is ambient,
    but a standup or 1:1 captured that way is a real conversation the user expects to
    find. The segmentation agent is what can tell the two apart, so promotion happens
    here rather than at ingest.

    Memory is deliberately *not* enqueued: these recordings are remembered through the
    settled-day episode pass (services/timeline/memory.py), not the per-conversation
    chain. Only title/summary runs, so a promoted recording is not left untitled.
    """

    cited: set[str] = set()
    for episode in episodes:
        if episode.conversational:
            cited |= _cited_conversation_ids(episode)
    if not cited:
        return []

    # A cited id names a container, and dedup, merge and trim all replace the container
    # while leaving the audio alone. Promoting only what is still live by that exact id
    # silently drops the meeting instead — see services/timeline/recording_refs.py.
    cited = await resolve_live_recordings(cited)
    if not cited:
        return []

    collection = Conversation.get_pymongo_collection()
    query = {
        "conversation_id": {"$in": sorted(cited)},
        "data_purpose": "capture_evidence",
    }
    promoted = [
        document["conversation_id"]
        async for document in collection.find(query, {"conversation_id": 1})
    ]
    if not promoted:
        return []

    await collection.update_many(
        {"conversation_id": {"$in": promoted}},
        {
            "$set": {
                "data_purpose": "conversation",
                "memory_excluded": False,
                "memory_exclusion_reason": None,
            }
        },
    )
    for conversation_id in promoted:
        default_queue.enqueue(
            generate_title_summary_job,
            conversation_id,
            job_timeout=300,
            result_ttl=JOB_RESULT_TTL,
            job_id=f"title_summary_{conversation_id[:12]}",
            description=f"Title/summary for promoted recording {conversation_id[:8]}",
            **post_conv_enqueue_kwargs(
                "title_summary", {"conversation_id": conversation_id}
            ),
        )
    logger.info(
        "🗣️ Promoted %d capture-evidence recording(s) to conversations: %s",
        len(promoted),
        ", ".join(item[:8] for item in promoted),
    )
    return promoted


async def _publish(
    run: TimelineAnalysisRun,
    manifest: TimelineEvidenceManifest,
    result: TimelineAgentResult,
    images: dict[str, bytes],
    pinned: list[TimelineEpisode] | None = None,
) -> None:
    evidence = {item.evidence_id: item for item in manifest.evidence}
    episode_ids = [str(uuid.uuid4()) for _ in result.episodes]
    documents: list[TimelineEpisode] = []
    for index, episode in enumerate(result.episodes):
        refs = [
            _evidence_ref(evidence[evidence_id]) for evidence_id in episode.evidence_ids
        ]
        representative = episode.representative_evidence_id
        document = TimelineEpisode(
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
            conversational=episode.conversational,
            salience=episode.salience,
            confidence=episode.confidence,
            activity_mode=episode.activity_mode,
            entities=episode.entities,
            attributes={item.key: item.value for item in episode.attributes},
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
        document.audio_ranges = await build_audio_ranges(
            started_at=document.started_at,
            ended_at=document.ended_at,
            evidence_refs=document.evidence_refs,
            related_conversation_ids=document.related_conversation_ids,
        )
        documents.append(document)
    carried = _carry_forward(run, pinned or [], manifest)
    await _guard_empty_generation(run, manifest, len(documents) + len(carried))
    if documents or carried:
        await TimelineEpisode.insert_many(documents + carried)
        # Promotion is one-way and idempotent: it only ever moves a recording out of
        # capture_evidence, and re-running finds nothing left to move. A later
        # generation that no longer calls the episode conversational therefore does not
        # re-hide a recording the user has already seen.
        await _promote_conversational_recordings(documents + carried)
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
    run.output_episode_ids = episode_ids + [episode.episode_id for episode in carried]


async def _process_run(run: TimelineAnalysisRun) -> None:
    settings = settings_dict()
    manifest, images = await assemble_day_evidence(
        run.user_id,
        run.local_date,
        run.timezone,
        window_minutes=int(settings.get("window_minutes", 20)),
        overlap_minutes=int(settings.get("overlap_minutes", 3)),
    )
    run.processed_evidence_revision = manifest.evidence_revision
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
    # Keyed on the confirmation timestamp, not ``status``: episodes published before
    # confirm-and-pin defaulted to "confirmed" without a person ever touching them, and
    # pinning those would freeze whole days against reanalysis.
    pinned = [episode for episode in existing if episode.confirmed_at is not None]
    with tempfile.TemporaryDirectory(prefix="chronicle-timeline-") as temp_dir:
        workspace = Path(temp_dir)
        write_workspace(
            workspace,
            manifest,
            max_text_chars_per_window=int(
                settings.get("max_text_chars_per_window", 30000)
            ),
            max_anchor_images_per_window=int(
                settings.get("max_anchor_images_per_window", 4)
            ),
        )
        with chronicle_span(
            "timeline.analyze_day",
            tracer_name="chronicle.timeline",
            attributes={
                "chronicle.timeline.run_id": run.run_id,
                "chronicle.timeline.local_date": str(run.local_date),
                "chronicle.timeline.timezone": run.timezone,
                "chronicle.timeline.executor": run.executor,
                "chronicle.timeline.prompt_version": run.prompt_version,
                "gen_ai.request.model": str(
                    (settings.get("codex") or {}).get("model") or ""
                ),
                "chronicle.timeline.evidence_count": len(manifest.evidence),
                "chronicle.timeline.window_count": len(manifest.windows),
                "chronicle.timeline.pinned_episodes": len(pinned),
                "user.id": run.user_id,
            },
        ) as span:
            result = await _analyze_with_escalation(
                manifest,
                workspace,
                existing,
                pinned,
                configured_effort=(settings.get("codex") or {}).get("reasoning_effort"),
                span=span,
            )
            set_span_usage(span, result.usage)
            set_span_attributes(
                span,
                {
                    "chronicle.timeline.episodes_drafted": len(result.episodes),
                    "chronicle.timeline.unassigned_intervals": len(
                        result.unassigned_intervals
                    ),
                },
            )
    run.state = "validating"
    run.usage = dict(result.usage)
    await run.save()
    # Already validated inside _analyze_with_escalation — it must run per attempt to
    # decide whether to retry, and it mutates the result, so a second call here would
    # double up the materialized unassigned intervals.
    await _publish(run, manifest, result, images, pinned)
    run.state = "complete"
    run.completed_at = utcnow()
    await run.save()


async def _run_claimed(run: TimelineAnalysisRun) -> dict[str, int]:
    """Execute one already-claimed run, recording how it ended."""

    settings = settings_dict()
    retry_hours = int((settings.get("codex") or {}).get("retry_hours", 6))
    try:
        await _process_run(run)
        return {"processed": 1, "failed": 0, "deferred": 0}
    except TimelineQuotaDeferred as error:
        run.state = "quota_deferred"
        run.retry_after = utcnow() + timedelta(hours=retry_hours)
        run.error = str(error)[:2000]
        run.usage = error.usage
        await run.save()
        return {"processed": 0, "failed": 0, "deferred": 1}
    except Exception as error:
        run.state = "failed"
        run.error = f"{type(error).__name__}: {error}"[:4000]
        run.completed_at = utcnow()
        await run.save()
        return {"processed": 0, "failed": 1, "deferred": 0}


async def process_timeline_analysis_runs(max_runs: int = 1) -> dict[str, int]:
    totals = {"processed": 0, "failed": 0, "deferred": 0}
    for _ in range(max_runs):
        run = await _claim_next_run()
        if run is None:
            break
        for key, value in (await _run_claimed(run)).items():
            totals[key] += value
    return totals


async def process_current_timeline_days() -> dict[str, int]:
    requested = 0
    users = await User.find({"timezone": {"$nin": [None, ""]}}).to_list()
    now = datetime.now(timezone.utc)
    for user in users:
        timezone_name = canonical_timezone(user.timezone)
        zone = ZoneInfo(timezone_name)
        today = now.astimezone(zone).date()
        # Current day gives prompt updates; previous day gets one final reconciliation
        # on the first scheduler tick after midnight, then evidence dedupe makes it free.
        for local_date in (today, today - timedelta(days=1)):
            await request_timeline_analysis(str(user.id), local_date, timezone_name)
            requested += 1
    result = await process_timeline_analysis_runs(max_runs=max(1, len(users)))
    return {"requested": requested, **result}
