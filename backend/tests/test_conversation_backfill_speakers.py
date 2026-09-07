"""Eligibility rules for the speaker-recognition backfill."""

from types import SimpleNamespace

from backend.controllers.conversation_controller import _needs_speaker_recognition


def _conversation(metadata, *, words=None, segments=None):
    version = (
        None
        if metadata is None
        else SimpleNamespace(
            metadata=metadata,
            words=words if words is not None else [{"word": "hi"}],
            segments=segments if segments is not None else [],
        )
    )
    return SimpleNamespace(active_transcript=version)


def test_transcript_the_speaker_step_never_saw_is_eligible():
    assert _needs_speaker_recognition(_conversation({"trigger": "upload"})) is True


def test_a_run_that_identified_nobody_is_not_re_run():
    """Zero identifications is an answer; re-running burns GPU for the same result."""

    conversation = _conversation(
        {"speaker_recognition": {"enabled": True, "speaker_count": 0}}
    )

    assert _needs_speaker_recognition(conversation) is False


def test_a_successful_run_is_not_re_run():
    conversation = _conversation(
        {"speaker_recognition": {"enabled": True, "speaker_count": 2}}
    )

    assert _needs_speaker_recognition(conversation) is False


def test_empty_metadata_is_eligible():
    assert _needs_speaker_recognition(_conversation({})) is True


def test_a_conversation_with_no_active_transcript_is_skipped():
    """Nothing to re-diarize, so it must not be counted as pending work forever."""

    assert _needs_speaker_recognition(_conversation(None)) is False


def test_a_transcript_with_nothing_to_diarize_is_excluded():
    """Neither words nor segments means this can never succeed — do not retry forever."""

    conversation = _conversation({}, words=[], segments=[])

    assert _needs_speaker_recognition(conversation) is False


def test_segments_alone_are_enough_to_diarize():
    conversation = _conversation({}, words=[], segments=[{"start": 0, "end": 1}])

    assert _needs_speaker_recognition(conversation) is True
