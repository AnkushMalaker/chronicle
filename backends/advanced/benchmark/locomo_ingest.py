"""LoCoMo ingestion — drives Chronicle's conversation memory path (``add_memory``).

A LoCoMo session is a single multi-speaker dialogue, so it maps onto one
``memory_service.add_memory(transcript, ...)`` call — the *same* entry point the
audio pipeline uses after diarization. This is deliberate: it exercises the
conversation-doc + person-note path the vault-first design targets, and it's
symmetric across both speakers (no user/assistant asymmetry like LongMemEval).

The transcript is speaker-labelled and headed with the session date so the
extractor gets temporal grounding. Note: the conversation-doc *frontmatter*
date is ingestion-time (``add_memory`` stamps ``now()``); the date that matters
for retrieval is the one inside the transcript/Key-Facts, which is the real
session date — that's what grep / search hits.

Ingestion is provider-agnostic: whichever ``MEMORY_PROVIDER`` / graph toggle
Chronicle is configured with handles storage. Cleanup reuses the LongMemEval
harness's ``cleanup_user`` (memory provider + KG + any chat sessions).
"""

from __future__ import annotations

import logging
import time

from advanced_omi_backend.services.memory import get_memory_service

from .locomo_loader import LocomoSession

logger = logging.getLogger(__name__)


def render_transcript(session: LocomoSession) -> str:
    """Render a session as a diarized, date-headed transcript string."""
    header = f"[Conversation recorded on {session.date_str}]" if session.date_str else "[Conversation]"
    lines = [header]
    for turn in session.turns:
        lines.append(f"{turn.speaker}: {turn.text}")
    return "\n".join(lines)


async def ingest_locomo_session(
    user_id: str,
    session: LocomoSession,
    user_email: str,
) -> tuple[str, int, bool]:
    """Ingest one LoCoMo session via ``add_memory``.

    Returns ``(session_id, chunk_count, success)``. ``chunk_count == 0`` flags a
    likely extraction failure (LoCoMo sessions are never legitimately empty).
    """
    transcript = render_transcript(session)
    memory_service = get_memory_service()

    t0 = time.perf_counter()
    success, chunk_ids = await memory_service.add_memory(
        transcript=transcript,
        client_id=user_id,
        source_id=session.session_id,
        user_id=user_id,
        user_email=user_email,
        allow_update=False,
    )
    logger.info(
        "ingest_locomo_session user=%s session=%s turns=%d chunks=%d ok=%s %.2fs",
        user_id,
        session.session_id,
        len(session.turns),
        len(chunk_ids),
        success,
        time.perf_counter() - t0,
    )
    return session.session_id, len(chunk_ids), success
