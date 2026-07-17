"""Settle conversations stuck in a non-terminal processing_status.

The status reconciler (services/status_reconciler.py) only scans non-deleted
conversations, and the no-speech/dead-end paths used to leave processing_status at
"active" (with a stale "Reprocessing..."/"Audio Recording (...)" title) — see the
fixes in mark_conversation_deleted and reprocess_speakers. That left a backlog of
conversations (mostly soft-deleted) stuck "active"/None/legacy "transcription_failed".

This one-off applies the SAME fact-derived logic (Conversation.apply_status, the single
owner of the field) to EVERY conversation — including deleted ones — and clears stale
placeholder titles. Idempotent: terminal, correctly-titled conversations are untouched.

Run inside the backend/worker container:
    uv run python3 scripts/settle_stuck_status.py            # dry run (default)
    uv run python3 scripts/settle_stuck_status.py --apply    # write changes
"""

import argparse
import asyncio
import sys

from beanie import init_beanie

from advanced_omi_backend.database import db
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User

TERMINAL = {
    Conversation.ConversationStatus.COMPLETED.value,
    Conversation.ConversationStatus.FAILED.value,
}
PLACEHOLDER_TITLES = ("Reprocessing...", "Recording...", "Transcribing...")


def _is_placeholder_title(title: str) -> bool:
    # "Audio Recording (Processing...)" / "(Batch Transcription...)" / "(Transcription
    # Failed)" — require the parenthetical so real LLM titles like "Audio Recording
    # Issues" are NOT matched.
    return "Audio Recording (" in title or title in PLACEHOLDER_TITLES


async def main(apply: bool) -> None:
    await init_beanie(database=db, document_models=[User, Conversation])

    scanned = settled = titled = 0
    async for conv in Conversation.find_all():
        scanned += 1
        changed = False

        # Only touch non-terminal conversations (active / None / legacy strings) —
        # leave already-settled completed/failed ones (and their titles) alone.
        if conv.processing_status not in TERMINAL:
            # Settle status from facts. apply_status: has transcript -> completed;
            # settled & none -> failed; else active.
            before = conv.processing_status
            if conv.apply_status(settled=True):
                settled += 1
                changed = True
                print(
                    f"  conv={conv.conversation_id[:12]} deleted={conv.deleted} "
                    f"status {before!r} -> {conv.processing_status!r}"
                    + (f" stage={conv.failure_stage}" if conv.failure_stage else "")
                )

            # Clear stale in-flight placeholder titles on these dead-end convs.
            title = conv.title or ""
            if title and _is_placeholder_title(title):
                conv.title = None
                titled += 1
                changed = True
                print(f"  conv={conv.conversation_id[:12]} cleared title {title!r}")

        if changed and apply:
            await conv.save()

    verb = "Settled" if apply else "Would settle"
    print(
        f"\nScanned {scanned} conversations. {verb} {settled} statuses, "
        f"cleared {titled} placeholder titles."
    )
    if not apply and (settled or titled):
        print("Dry run — re-run with --apply to write changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry run)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply))
    sys.exit(0)
