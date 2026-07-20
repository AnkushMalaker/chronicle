"""The no-data-loss guard for conversations whose audio never persisted.

When a conversation's audio chunks are missing at finalize (e.g. a mid-session
reconnect routed the audio to the session's always_persist placeholder under a
different conversation_id), the conversation must NOT be discarded if it carries a
real transcript — losing a real transcript is worse than keeping an audio-less
conversation. Only a genuinely empty conversation (no meaningful transcript) is
discarded.
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
