"""The no-data-loss guard for conversations whose audio never persisted.

When a Conversation's capture claim cannot be attached at finalize, it must NOT be
discarded if it carries a real transcript. Losing a real transcript is worse than
keeping an audio-less semantic record. Only a genuinely empty Conversation (no
meaningful transcript) is discarded.
"""

import pytest

from advanced_omi_backend.workers.conversation_jobs import (
    should_discard_unbacked_conversation,
)

pytestmark = pytest.mark.unit


def test_empty_conversation_is_discarded():
    # No transcript and no audio → nothing worth keeping.
    assert should_discard_unbacked_conversation(has_meaningful_transcript=False) is True


def test_transcript_bearing_conversation_is_kept():
    # Real transcript but missing audio → keep it (don't lose the transcript).
    assert should_discard_unbacked_conversation(has_meaningful_transcript=True) is False
