"""Backfill renderable segments for transcript versions that have text but no segments.

The streaming transcription path used to store a transcript version with text + words
but an EMPTY segments list when the provider didn't diarize (and speaker recognition was
off/unavailable). The web UI only renders the segment list, so those conversations showed
"No transcript segments available" despite having a transcript.

This script finds every transcript version where `transcript` has text but `segments` is
empty, and builds a single fallback "Speaker 0" segment from the version's words (or the
full text). It is idempotent — versions that already have segments are left untouched.

Run inside the backend/worker container:
    uv run python3 scripts/backfill_empty_segments.py            # dry run (default)
    uv run python3 scripts/backfill_empty_segments.py --apply    # write changes
"""

import argparse
import asyncio
import sys

from beanie import init_beanie

from advanced_omi_backend.database import db
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User


def _build_fallback_segment(version) -> "Conversation.SpeakerSegment":
    """Build a single full-span fallback segment from a version's words/text."""
    words = version.words or []
    start = words[0].start if words else 0.0
    end = words[-1].end if words else 0.0
    return Conversation.SpeakerSegment(
        speaker="Speaker 0",
        start=start,
        end=end,
        text=version.transcript or "",
        words=list(words),
    )


async def main(apply: bool) -> None:
    await init_beanie(database=db, document_models=[User, Conversation])

    scanned = 0
    fixed_versions = 0
    fixed_convs = 0

    async for conv in Conversation.find_all():
        scanned += 1
        changed = False
        for version in conv.transcript_versions:
            has_text = bool((version.transcript or "").strip())
            if has_text and not version.segments:
                seg = _build_fallback_segment(version)
                version.segments = [seg]
                # Mark provenance so it's distinguishable from real diarization
                version.diarization_source = version.diarization_source or None
                if isinstance(version.metadata, dict):
                    version.metadata.setdefault(
                        "segments_created_by", "backfill_fallback"
                    )
                fixed_versions += 1
                changed = True
                print(
                    f"  conv={conv.conversation_id[:12]} version={version.version_id} "
                    f"-> 1 fallback segment ({len(seg.text)} chars, {len(seg.words)} words)"
                )
        if changed:
            fixed_convs += 1
            if apply:
                await conv.save()

    print(
        f"\nScanned {scanned} conversations. "
        f"{'Fixed' if apply else 'Would fix'} {fixed_versions} versions "
        f"across {fixed_convs} conversations."
    )
    if not apply and fixed_versions:
        print("Dry run — re-run with --apply to write changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry run)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply))
    sys.exit(0)
