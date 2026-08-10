"""Periodic reclamation of raw-audio write-ahead logs whose recording is over.

Stream deletion is otherwise attempted at exactly one moment per recording: when
the streaming consumer or the session finalizer reaches the end of it. A process
restart, worker rotation, or crash between those points leaves the stream behind,
and nothing ever asks again — there is no retry and no age-based expiry, because
the module deliberately refuses to treat age as proof of safety.

The result is silent, unbounded accumulation. This deployment had 11 such streams
holding **764 MB** of Redis: five whose session hash was already gone, and six that
were fully drained and safe to delete for as long as 31 hours.

This sweep introduces no new deletion policy. It re-asks the same proven-safe
question: :func:`delete_stream_if_durable` still requires every registered consumer
group to show zero pending and zero lag, and the persistence consumer acknowledges
only after a journaled Mongo commit.
"""

import logging

from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
    parse_consumer_groups,
    session_append_closed,
)
from advanced_omi_backend.services.observability.system_events import record_event

logger = logging.getLogger(__name__)

# Streams reclaimed per tick. A WAL can be hundreds of megabytes, and reclaiming
# one is a Redis-side operation on a single large key; a bounded batch keeps any
# one tick's impact on the server small. What is skipped is retried next tick.
MAX_STREAMS_PER_SWEEP = 5

# A consumer idle this long with nothing pending has no state left to lose — it is
# the registry entry of a worker that has moved on, and Redis recreates it on the
# next read. A consumer with pending entries is never touched here, however idle:
# only the consumer that committed the side effect may acknowledge its messages.
IDLE_CONSUMER_SECONDS = 300


async def _drop_finished_consumers(redis_client, stream_name: str) -> int:
    """Remove the registry entries of consumers with nothing left to do."""
    dropped = 0
    raw_groups = await redis_client.execute_command("XINFO", "GROUPS", stream_name)
    for group in parse_consumer_groups(raw_groups or []):
        consumers = await redis_client.execute_command(
            "XINFO", "CONSUMERS", stream_name, group
        )
        for consumer in consumers or []:
            values = {
                _text(consumer[i]): consumer[i + 1] for i in range(0, len(consumer), 2)
            }
            name = _text(values.get("name", ""))
            if int(values.get("pending", 0)) != 0:
                continue
            if int(values.get("idle", 0)) <= IDLE_CONSUMER_SECONDS * 1000:
                continue
            await redis_client.execute_command(
                "XGROUP", "DELCONSUMER", stream_name, group, name
            )
            dropped += 1
    return dropped


def _text(value) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _incident_key(stream_name: str) -> str:
    return f"audio-stream-backlog:{stream_name}"


async def _report_blocked_incident(stream_name: str, reason: str) -> None:
    """Raise a *durable incident*, not a log line per sweep.

    A finished recording whose consumers have not drained is a standing fault: the
    audio was delivered to a consumer that never acknowledged it, so it was never
    processed and its WAL can never be freed. No cleanup can resolve that — only the
    consumer that committed the side effect may acknowledge its own messages — so it
    needs an operator, which means it must be visible without being noise.

    Plain ``logger.error`` would be noise: the system-event handler collapses
    identical events only within a 30-second window, and this sweep runs every 15
    minutes, so one stuck stream would file ~96 error rows a day. An incident key
    keeps it to one open row that accrues occurrences until it is resolved.
    """
    await record_event(
        severity="error",
        category="audio",
        source=__name__,
        title=f"Audio stream blocked by an undrained consumer: {reason}",
        detail=(
            f"{stream_name} cannot be reclaimed while a consumer holds unacknowledged "
            f"entries ({reason}). Those entries were delivered but never processed, so "
            f"that audio is missing from the transcript and the log cannot be freed."
        ),
        incident_key=_incident_key(stream_name),
    )


async def _resolve_blocked_incident(stream_name: str) -> None:
    """Close the incident once the stream drains. A no-op if none is open."""
    await record_event(
        severity="info",
        category="audio",
        source=__name__,
        title=f"Audio stream reclaimed: {stream_name}",
        incident_key=_incident_key(stream_name),
        resolves_incident=True,
    )


async def reclaim_settled_audio_streams(
    max_streams: int = MAX_STREAMS_PER_SWEEP,
) -> dict:
    """Reclaim what a finished recording leaves behind: its WAL and its consumers."""
    redis_client = create_async_redis()
    examined = 0
    reclaimed = 0
    dropped_consumers = 0
    retained: dict = {}
    blocked: dict = {}
    try:
        for stream_key in await redis_client.keys("audio:stream:*"):
            if reclaimed >= max_streams:
                logger.info(
                    f"Stream reclaim hit its per-sweep bound of {max_streams}; "
                    f"the rest are retried next tick"
                )
                break

            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )
            session_id = stream_name.removeprefix("audio:stream:")
            examined += 1

            try:
                dropped_consumers += await _drop_finished_consumers(
                    redis_client, stream_name
                )
            except Exception as e:  # noqa: BLE001 — hygiene must not block reclaim
                logger.warning(f"Could not tidy consumers on {stream_name}: {e}")

            if not await session_append_closed(redis_client, session_id):
                retained[stream_name] = "session_may_still_append"
                continue

            decision = await delete_stream_if_durable(
                redis_client,
                stream_name,
                required_groups={AUDIO_PERSISTENCE_GROUP},
            )
            if decision.safe_to_delete:
                reclaimed += 1
                logger.info(f"Reclaimed settled audio stream {stream_name}")
                await _resolve_blocked_incident(stream_name)
            else:
                retained[stream_name] = decision.reason
                blocked[stream_name] = decision.reason
                await _report_blocked_incident(stream_name, decision.reason)
    finally:
        await redis_client.aclose()

    return {
        "examined": examined,
        "reclaimed": reclaimed,
        "retained": len(retained),
        "dropped_consumers": dropped_consumers,
        "blocked": blocked,
    }
