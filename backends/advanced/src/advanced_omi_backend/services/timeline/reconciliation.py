"""One reconciliation run over one dirty evidence range.

A run starts from the dirty interval plus five minutes of context per side, asks the
agent to *revise* the prior interpretation rather than rederive it, and either
publishes a revisioned generation, asks for bounded expansion, or parks until future
evidence exists.

Chronicle owns the budgets and the fencing; the agent owns the semantics:

- expansion is at most ``EXPANSION_STEP`` per requested side per iteration and at most
  ``MAX_EXPANSIONS`` iterations, so one run can never widen more than 30 minutes a side;
- a publish is validated by the same deterministic guards the day pipeline uses, with
  exactly one validation-feedback retry;
- an episode that crosses a human-pinned boundary is rejected the same way;
- the publish compare-and-swaps on the episode revisions the bundle saw, so a run
  holding a stale leased snapshot cannot overwrite newer work.

Publish ordering (deliberate, see :func:`publish_reconciliation`):

1. read the current rows and compare them with the revisions the bundle observed;
2. conditionally supersede each carried row on ``(episode_id, revision)`` — a failed
   condition is a lost race, so already-applied supersessions are restored and the run
   reports ``fenced=False``;
3. only then insert the new rows.

Nothing is inserted until every fence check has passed, so a fenced run leaves the
active generation intact and the re-dirtied range simply runs again.

See ``docs/backend/rolling-reconciliation.md`` → "Agentic context expansion",
"Episode revisions and stable navigation", and "Settlement is a policy decision".
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional, Sequence
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    TimelineAssertion,
    TimelineEpisode,
    TimelineEvidenceRef,
    utcnow,
)
from advanced_omi_backend.models.user import User

from .contracts import (
    AgentEpisode,
    EvidenceBundle,
    Publish,
    PublishResult,
    ReconcileAction,
    RequestMoreContext,
    TimelineAgentResult,
    WaitForFutureEvidence,
)
from .dirty_ranges import LEASE_MINUTES, MAX_ATTEMPTS, complete_range, park_waiting
from .episode_bounds import speech_profile_for_range
from .evidence import load_reconciliation_evidence
from .executor import (
    TimelineIncompleteSegmentation,
    build_range_executor,
    validate_agent_result,
)
from .projection import affected_local_dates, refresh_projections
from .recording_refs import build_audio_ranges

logger = logging.getLogger(__name__)

# Context Chronicle adds around the dirty interval before the first agent look.
CONTEXT_PADDING = timedelta(minutes=5)
# One expansion iteration may widen a requested side by at most this much; a bigger
# ask is clamped rather than refused.
EXPANSION_STEP = timedelta(minutes=5)
# At most six expansions ⇒ at most thirty minutes per side in one run.
MAX_EXPANSIONS = 6
# Settlement watermark: an episode ending within this of the newest evidence is still
# at the live edge, and this is also the quiet window a settled boundary needs.
SETTLE_QUIET_MINUTES = 10

SettlementStatus = Literal["open", "provisional", "settled"]

LoadEvidence = Callable[..., Awaitable[EvidenceBundle]]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _evidence_end(item: Any) -> datetime:
    return _utc(item.ended_at or item.started_at)


async def _user_timezone(user_id: str) -> str:
    """The user's IANA timezone, or UTC when unknown."""

    try:
        user = await User.get(user_id)
    except Exception:
        logger.debug("Could not resolve timezone for user %s", user_id, exc_info=True)
        return "UTC"
    return (user.timezone if user and user.timezone else None) or "UTC"


# ── Leasing one named range ──────────────────────────────────────────────────


async def lease_range_by_id(
    dirty_range_id: str, owner: str, now: Optional[datetime] = None
) -> Optional[DirtyEvidenceRange]:
    """CAS-claim this specific range, or re-adopt a lease this owner already holds.

    ``dirty_ranges.lease_due_range`` deliberately claims the globally oldest due range,
    which is the wrong shape for a job that was enqueued *for one id*. Same CAS pattern,
    scoped to the id.
    """

    now = _utc(now) if now else utcnow()
    collection = DirtyEvidenceRange.get_pymongo_collection()
    document = await collection.find_one_and_update(
        {
            "dirty_range_id": dirty_range_id,
            "attempts": {"$lt": MAX_ATTEMPTS},
            "$or": [
                {"state": {"$in": ["pending", "waiting"]}},
                {"state": "leased", "lease_owner": owner},
                {"state": "leased", "lease_expires_at": {"$lt": now}},
            ],
        },
        [
            {
                "$set": {
                    "state": "leased",
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(minutes=LEASE_MINUTES),
                    "attempts": {"$add": ["$attempts", 1]},
                    "leased_evidence_revision": "$evidence_revision",
                    "last_error": None,
                    "updated_at": now,
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )


# ── Settlement policy v1 ─────────────────────────────────────────────────────


def _newest_evidence_at(bundle: EvidenceBundle) -> datetime:
    ends = [_evidence_end(item) for item in bundle.manifest.evidence]
    return max(ends) if ends else _utc(bundle.manifest.ended_at)


def _closed_by_meeting(episode: AgentEpisode, bundle: EvidenceBundle) -> bool:
    """A meeting evidence item ending at this boundary is an authoritative close."""

    end = _utc(episode.ended_at)
    tolerance = timedelta(minutes=2)
    return any(
        item.kind == "meeting" and abs(_evidence_end(item) - end) <= tolerance
        for item in bundle.manifest.evidence
    )


def _evidence_after(
    bundle: EvidenceBundle, start: datetime, end: datetime
) -> list[Any]:
    return [
        item
        for item in bundle.manifest.evidence
        if item.kind != "capture_gap"
        and _utc(item.started_at) < end
        and _evidence_end(item) > start
    ]


async def _pending_prerequisites(user_id: str, start: datetime, end: datetime) -> bool:
    """Whether a known artifact for this interval has not finished yet.

    Exactly two queries, both scoped to intervals intersecting ``[start, end)``:

    - a ``Conversation`` whose ``processing_status`` is ``active`` or unset — its
      transcript, speakers, or summary may still change the range's meaning;
    - an ``AudioEvidenceSpan`` in state ``unscored`` — audio Chronicle has stored but
      has not profiled, so its silence and speech are unknown rather than quiet.
    """

    conversations = await Conversation.find(
        {
            "user_id": user_id,
            "started_at": {"$lt": end},
            "ended_at": {"$gt": start},
            "$or": [
                {"processing_status": Conversation.ConversationStatus.ACTIVE.value},
                {"processing_status": None},
                {"processing_status": {"$exists": False}},
            ],
        }
    ).count()
    if conversations:
        return True
    spans = await AudioEvidenceSpan.find(
        {
            "user_id": user_id,
            "state": "unscored",
            "started_at": {"$lt": end},
            "ended_at": {"$gt": start},
        }
    ).count()
    return bool(spans)


async def _boundary_is_quiet(end: datetime, window: timedelta) -> Optional[bool]:
    """Whether VAD says the ``window`` after ``end`` is quiet.

    ``None`` means the audio was never measured there, so the caller falls back to the
    evidence watermark alone.
    """

    try:
        profile = await speech_profile_for_range(end, end + window)
    except Exception:
        logger.debug("Speech profile unavailable at %s", end.isoformat(), exc_info=True)
        return None
    if not profile or not profile.measured_buckets:
        return None
    return profile.speech_buckets == 0


async def assess_settlement(
    episode: AgentEpisode,
    bundle: EvidenceBundle,
    now: Optional[datetime] = None,
    *,
    user_id: Optional[str] = None,
) -> SettlementStatus:
    """Settlement policy v1 for one published episode.

    ``open`` when the episode's right edge sits within ``SETTLE_QUIET_MINUTES`` of the
    newest evidence in the bundle — evidence is still accumulating there. Otherwise
    ``settled`` when both (a) ten quiet minutes follow the end, or a meeting bound
    closes it, and (b) no pending prerequisite intersects the interval; else
    ``provisional``.

    Where stored VAD covers the minutes after the boundary, quiet is confirmed from the
    bucket series rather than from the absence of assembled evidence; where it does not,
    the evidence watermark decides alone.
    """

    now = _utc(now) if now else utcnow()
    quiet = timedelta(minutes=SETTLE_QUIET_MINUTES)
    start, end = _utc(episode.started_at), _utc(episode.ended_at)
    horizon = min(_newest_evidence_at(bundle), now)

    if horizon - end < quiet:
        return "open"

    closed = _closed_by_meeting(episode, bundle)
    if not closed:
        if _evidence_after(bundle, end, end + quiet):
            return "provisional"
        measured_quiet = await _boundary_is_quiet(end, quiet)
        if measured_quiet is False:
            return "provisional"

    if user_id and await _pending_prerequisites(user_id, start, end):
        return "provisional"
    return "settled"


# ── Publishing one generation ────────────────────────────────────────────────


def _evidence_ref(item: Any) -> TimelineEvidenceRef:
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


async def _prior_rows(bundle: EvidenceBundle) -> list[TimelineEpisode]:
    episode_ids = [
        str(item.get("episode_id"))
        for item in bundle.existing_episodes
        if item.get("episode_id")
    ]
    if not episode_ids:
        return []
    return await TimelineEpisode.find({"episode_id": {"$in": episode_ids}}).to_list()


async def observed_revisions(bundle: EvidenceBundle) -> dict[str, int]:
    """The ``(episode_id → revision)`` snapshot this bundle reflects.

    ``load_reconciliation_evidence`` serializes only enough of a prior episode for the
    agent to read, so the fence's reference point is captured here, beside the load,
    rather than reconstructed at publish time when it would no longer be a snapshot.
    """

    return {row.episode_id: int(row.revision) for row in await _prior_rows(bundle)}


def _overlaps(
    first: tuple[datetime, datetime], second: tuple[datetime, datetime]
) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _components(
    priors: Sequence[TimelineEpisode], episodes: Sequence[AgentEpisode]
) -> list[tuple[list[int], list[int]]]:
    """Connected prior/new groups over interval overlap.

    A component with one prior and one new episode is a revision; one prior and several
    new episodes is a split; several priors and one new episode is a merge. A component
    with no prior mints a new key, and a prior nobody overlaps is left alone.
    """

    prior_spans = [(_utc(row.started_at), _utc(row.ended_at)) for row in priors]
    new_spans = [(_utc(item.started_at), _utc(item.ended_at)) for item in episodes]
    edges: dict[int, set[int]] = {index: set() for index in range(len(priors))}
    reverse: dict[int, set[int]] = {index: set() for index in range(len(episodes))}
    for prior_index, prior_span in enumerate(prior_spans):
        for new_index, new_span in enumerate(new_spans):
            if _overlaps(prior_span, new_span):
                edges[prior_index].add(new_index)
                reverse[new_index].add(prior_index)

    seen_priors: set[int] = set()
    seen_new: set[int] = set()
    components: list[tuple[list[int], list[int]]] = []
    for new_index in range(len(episodes)):
        if new_index in seen_new:
            continue
        prior_group: set[int] = set()
        new_group: set[int] = {new_index}
        frontier = [new_index]
        seen_new.add(new_index)
        while frontier:
            current = frontier.pop()
            for prior_index in reverse[current]:
                if prior_index in prior_group:
                    continue
                prior_group.add(prior_index)
                seen_priors.add(prior_index)
                for sibling in edges[prior_index]:
                    if sibling not in new_group:
                        new_group.add(sibling)
                        seen_new.add(sibling)
                        frontier.append(sibling)
        components.append((sorted(prior_group), sorted(new_group)))
    return components


def _unchanged(prior: TimelineEpisode, episode: AgentEpisode) -> bool:
    """Whether the agent handed back the prior episode verbatim."""

    return (
        _utc(prior.started_at) == _utc(episode.started_at)
        and _utc(prior.ended_at) == _utc(episode.ended_at)
        and prior.kind == episode.kind
        and prior.title == episode.title
        and prior.summary == episode.summary
        and prior.conversational == episode.conversational
        and prior.salience == episode.salience
        and prior.activity_mode == episode.activity_mode
    )


async def _build_document(
    *,
    episode: AgentEpisode,
    user_id: str,
    run_id: str,
    timezone_name: str,
    bundle: EvidenceBundle,
    episode_key: str,
    revision: int,
    predecessor_keys: list[str],
    status: SettlementStatus,
    evidence_revision: Optional[int],
) -> TimelineEpisode:
    evidence = {item.evidence_id: item for item in bundle.manifest.evidence}
    refs = [
        _evidence_ref(evidence[evidence_id])
        for evidence_id in episode.evidence_ids
        if evidence_id in evidence
    ]
    zone = ZoneInfo(timezone_name)
    document = TimelineEpisode(
        episode_id=str(uuid.uuid4()),
        episode_key=episode_key,
        run_id=run_id,
        user_id=user_id,
        local_date=_utc(episode.started_at).astimezone(zone).date(),
        timezone=timezone_name,
        started_at=_utc(episode.started_at),
        ended_at=_utc(episode.ended_at),
        kind=episode.kind,
        title=episode.title,
        summary=episode.summary,
        conversational=episode.conversational,
        status=status,
        pipeline="rolling",
        revision=revision,
        evidence_revision=evidence_revision,
        predecessor_keys=predecessor_keys,
        salience=episode.salience,
        confidence=episode.confidence,
        activity_mode=episode.activity_mode,
        entities=list(episode.entities),
        attributes={item.key: item.value for item in episode.attributes},
        assertions=[
            TimelineAssertion(**assertion.model_dump())
            for assertion in episode.assertions
        ],
        evidence_refs=refs,
        source_ids=sorted({ref.source_id for ref in refs if ref.source_id}),
        related_conversation_ids=list(episode.related_conversation_ids),
    )
    document.audio_ranges = await build_audio_ranges(
        started_at=document.started_at,
        ended_at=document.ended_at,
        evidence_refs=document.evidence_refs,
        related_conversation_ids=document.related_conversation_ids,
    )
    return document


async def publish_reconciliation(
    user_id: str,
    dirty_range: DirtyEvidenceRange,
    bundle: EvidenceBundle,
    result: TimelineAgentResult,
    *,
    observed: Optional[dict[str, int]] = None,
    timezone_name: Optional[str] = None,
    refresh_projections_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    now: Optional[datetime] = None,
) -> PublishResult:
    """Atomically publish one reconciliation generation, fenced on prior revisions.

    See the module docstring for the write ordering and why nothing is inserted before
    every fence check has passed.
    """

    timezone_name = timezone_name or await _user_timezone(user_id)
    priors = await _prior_rows(bundle)
    observed = (
        observed
        if observed is not None
        else {row.episode_id: int(row.revision) for row in priors}
    )
    evidence_revision = dirty_range.leased_evidence_revision

    # (1) Compare the current rows against the snapshot the bundle reflects. A row that
    # moved on, or one written by a *newer* evidence revision, means this run is stale.
    for row in priors:
        expected = observed.get(row.episode_id)
        if expected is not None and int(row.revision) != expected:
            logger.info(
                "🩹 Fenced: episode %s moved to revision %s (bundle saw %s)",
                row.episode_id,
                row.revision,
                expected,
            )
            return PublishResult(fenced=False)
        if (
            evidence_revision is not None
            and row.evidence_revision is not None
            and int(row.evidence_revision) > int(evidence_revision)
        ):
            logger.info(
                "🩹 Fenced: episode %s carries evidence revision %s, newer than the "
                "leased snapshot %s",
                row.episode_id,
                row.evidence_revision,
                evidence_revision,
            )
            return PublishResult(fenced=False)

    run_id = f"rolling:{dirty_range.dirty_range_id}"
    documents: list[TimelineEpisode] = []
    supersessions: list[tuple[TimelineEpisode, list[str]]] = []

    for prior_indices, new_indices in _components(priors, result.episodes):
        group_priors = [priors[index] for index in prior_indices]
        group_new = [result.episodes[index] for index in new_indices]

        if len(group_priors) == 1 and len(group_new) == 1:
            prior, episode = group_priors[0], group_new[0]
            if _unchanged(prior, episode):
                # Carry-forward: same key, same revision, no row written at all.
                continue
            status = await assess_settlement(episode, bundle, now, user_id=user_id)
            documents.append(
                await _build_document(
                    episode=episode,
                    user_id=user_id,
                    run_id=run_id,
                    timezone_name=timezone_name,
                    bundle=bundle,
                    episode_key=prior.episode_key,
                    revision=int(prior.revision) + 1,
                    predecessor_keys=list(prior.predecessor_keys),
                    status=status,
                    evidence_revision=evidence_revision,
                )
            )
            # A same-key revision needs no successor pointer: resolving the key finds
            # the highest revision. Only split/merge changes which key to resolve.
            supersessions.append((prior, []))
            continue

        predecessor_keys = [prior.episode_key for prior in group_priors]
        minted: list[str] = []
        for episode in group_new:
            episode_key = str(uuid.uuid4())
            minted.append(episode_key)
            status = await assess_settlement(episode, bundle, now, user_id=user_id)
            documents.append(
                await _build_document(
                    episode=episode,
                    user_id=user_id,
                    run_id=run_id,
                    timezone_name=timezone_name,
                    bundle=bundle,
                    episode_key=episode_key,
                    revision=1,
                    predecessor_keys=predecessor_keys,
                    status=status,
                    evidence_revision=evidence_revision,
                )
            )
        for prior in group_priors:
            supersessions.append((prior, minted))

    if not documents:
        logger.debug(
            "🩹 Reconciliation of %s produced no material change",
            dirty_range.dirty_range_id,
        )
        return PublishResult(fenced=True, material_change=False)

    # (2) Conditionally supersede every carried row before inserting anything. A failed
    # condition is a lost race with a newer generation: restore what was already
    # superseded and report the fence rather than writing a half generation.
    collection = TimelineEpisode.get_pymongo_collection()
    applied: list[TimelineEpisode] = []
    stamp = _utc(now) if now else utcnow()
    for prior, successors in supersessions:
        updated = await collection.update_one(
            {"episode_id": prior.episode_id, "revision": int(prior.revision)},
            {
                "$set": {
                    "status": "superseded",
                    "successor_keys": successors,
                    "revised_at": stamp,
                }
            },
        )
        if updated.matched_count == 0:
            for reverted in applied:
                await collection.update_one(
                    {"episode_id": reverted.episode_id},
                    {
                        "$set": {
                            "status": reverted.status,
                            "successor_keys": list(reverted.successor_keys),
                            "revised_at": reverted.revised_at,
                        }
                    },
                )
            logger.info(
                "🩹 Fenced while superseding %s; restored %d row(s)",
                prior.episode_id,
                len(applied),
            )
            return PublishResult(fenced=False)
        applied.append(prior)

    # (3) Every fence has passed; the new generation can land.
    await TimelineEpisode.insert_many(documents)

    dates: list[Any] = []
    for document in documents:
        for local_date in affected_local_dates(
            document.started_at, document.ended_at, timezone_name
        ):
            if local_date not in dates:
                dates.append(local_date)
    dates.sort()

    refresh = refresh_projections_fn or refresh_projections
    try:
        await refresh(user_id, dates, timezone_name=timezone_name)
    except Exception:
        # A projection is derived state; failing to refresh it must not undo a valid
        # publish. The day audit regenerates it.
        logger.warning(
            "🩹 Projection refresh failed after publishing %s",
            dirty_range.dirty_range_id,
            exc_info=True,
        )

    return PublishResult(
        episode_ids=[document.episode_id for document in documents],
        episode_keys=[document.episode_key for document in documents],
        superseded_episode_ids=[prior.episode_id for prior, _ in supersessions],
        affected_local_dates=dates,
        fenced=True,
        material_change=True,
    )


# ── The run ──────────────────────────────────────────────────────────────────


def _pinned_intervals(bundle: EvidenceBundle) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    for item in bundle.pinned_episodes:
        try:
            intervals.append(
                (
                    _utc(datetime.fromisoformat(str(item["started_at"]))),
                    _utc(datetime.fromisoformat(str(item["ended_at"]))),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("🩹 Ignoring unparseable pinned episode %s", item)
    return intervals


def crossed_pins(
    result: TimelineAgentResult, bundle: EvidenceBundle
) -> list[tuple[int, tuple[datetime, datetime]]]:
    """Published episodes that run through a pinned boundary instead of up to it."""

    pins = _pinned_intervals(bundle)
    crossings: list[tuple[int, tuple[datetime, datetime]]] = []
    for index, episode in enumerate(result.episodes):
        start, end = _utc(episode.started_at), _utc(episode.ended_at)
        for pin in pins:
            if start < pin[0] < end or start < pin[1] < end:
                crossings.append((index, pin))
                break
    return crossings


def _expand(
    bounds: tuple[datetime, datetime], action: RequestMoreContext
) -> tuple[datetime, datetime]:
    """Widen only the requested sides, clamped to one step each."""

    step = EXPANSION_STEP.total_seconds()
    left = min(float(action.left_seconds), step)
    right = min(float(action.right_seconds), step)
    return (
        bounds[0] - timedelta(seconds=left),
        bounds[1] + timedelta(seconds=right),
    )


async def reconcile_range(
    dirty_range: DirtyEvidenceRange,
    *,
    executor: Any = None,
    load_evidence: Optional[LoadEvidence] = None,
    refresh_projections: Optional[Callable[..., Awaitable[Any]]] = None,
    now: Optional[datetime] = None,
    accounting: Optional[list[dict[str, Any]]] = None,
) -> Optional[PublishResult]:
    """Reconcile one leased dirty range. ``None`` means the range was parked.

    ``accounting`` is an optional caller-supplied sink for the exact inspected-evidence
    record — one entry per iteration with its bounds and evidence count. The same record
    is logged; the sink exists so a caller (and a test) can assert on it without
    scraping logs.
    """

    load = load_evidence or load_reconciliation_evidence
    runner = executor if executor is not None else build_range_executor()
    timezone_name = await _user_timezone(dirty_range.user_id)
    log: list[dict[str, Any]] = accounting if accounting is not None else []

    bounds = (
        _utc(dirty_range.started_at) - CONTEXT_PADDING,
        _utc(dirty_range.ended_at) + CONTEXT_PADDING,
    )
    expansions = 0
    validation_feedback: Optional[str] = None
    retried_validation = False
    bundle: Optional[EvidenceBundle] = None
    snapshot: dict[str, int] = {}

    while True:
        if bundle is None:
            bundle = await load(
                dirty_range.user_id,
                bounds[0],
                bounds[1],
                timezone_name=timezone_name,
                evidence_revision=dirty_range.leased_evidence_revision or 0,
            )
            snapshot = await observed_revisions(bundle)
            log.append(
                {
                    "iteration": len(log),
                    "started_at": bounds[0].isoformat(),
                    "ended_at": bounds[1].isoformat(),
                    "evidence_count": len(bundle.manifest.evidence),
                }
            )

        action: ReconcileAction = await runner.reconcile(
            bundle, validation_feedback=validation_feedback
        )

        if isinstance(action, WaitForFutureEvidence):
            logger.info(
                "🩹 Range %s waits for future evidence: %s",
                dirty_range.dirty_range_id,
                action.reason,
            )
            await park_waiting(dirty_range, action.reason)
            return None

        if isinstance(action, RequestMoreContext):
            if expansions >= MAX_EXPANSIONS:
                reason = (
                    f"expansion budget exhausted after {expansions} iteration(s); "
                    f"last ask: {action.reason}"
                )
                logger.info(
                    "🩹 Range %s parked: %s", dirty_range.dirty_range_id, reason
                )
                await park_waiting(dirty_range, "expansion budget exhausted")
                return None
            bounds = _expand(bounds, action)
            expansions += 1
            bundle = None
            validation_feedback = None
            continue

        assert isinstance(action, Publish)
        result = action.result
        try:
            validate_agent_result(
                result,
                bundle.manifest,
                _pinned_intervals(bundle),
                salvage_gap_bridging_episodes=retried_validation,
            )
            crossings = crossed_pins(result, bundle)
            if crossings:
                index, pin = crossings[0]
                raise TimelineIncompleteSegmentation(
                    f"episode {index} crosses the pinned boundary "
                    f"{pin[0].isoformat()}–{pin[1].isoformat()}; end the episode at the "
                    "pin instead of running through it"
                )
        except (TimelineIncompleteSegmentation, ValueError) as error:
            if retried_validation:
                logger.warning(
                    "🩹 Range %s parked after a second invalid draft: %s",
                    dirty_range.dirty_range_id,
                    error,
                )
                await park_waiting(dirty_range, f"invalid segmentation: {error}")
                return None
            retried_validation = True
            validation_feedback = str(error)
            continue

        logger.info(
            "🩹 Range %s publishing %d episode(s) after %d expansion(s): %s",
            dirty_range.dirty_range_id,
            len(result.episodes),
            expansions,
            log,
        )
        return await publish_reconciliation(
            dirty_range.user_id,
            dirty_range,
            bundle,
            result,
            observed=snapshot,
            timezone_name=timezone_name,
            refresh_projections_fn=refresh_projections,
            now=now,
        )


async def finish_range(
    dirty_range: DirtyEvidenceRange, outcome: Optional[PublishResult]
) -> str:
    """Terminate a leased range from a reconciliation outcome.

    ``None`` means the run already parked the range. A fenced publish is not a failure:
    the trigger that raced this run left a fresh pending range, so the interval is
    reconciled again with the newer snapshot.
    """

    if outcome is None:
        return dirty_range.state
    if not outcome.fenced:
        await park_waiting(dirty_range, "fenced by a newer evidence revision")
        return "waiting"
    await complete_range(dirty_range)
    return "completed"
