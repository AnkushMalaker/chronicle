"""A split child must carry the parent's whole version chain, not one slice.

Rebuild walks a conversation's own versions back to the underlying ASR
transcript. If a child holds only a slice of whatever happened to be active,
that walk stops on speaker-labelled text and treats it as clean ASR.
"""

from backend.controllers.data_audit_controller import (
    _asr_source_version,
    _avoid_speech_segments,
    _slice_versions_onto_child,
)
from backend.models.conversation import Conversation


def _segment(start: float, end: float, speaker: str, text: str):
    return Conversation.SpeakerSegment(start=start, end=end, speaker=speaker, text=text)


def _blank(conversation_id: str) -> Conversation:
    # model_construct: this exercises pure version arithmetic, so the test needs
    # the model's behaviour without a Beanie collection behind it.
    return Conversation.model_construct(
        conversation_id=conversation_id,
        audio_uuid=conversation_id,
        user_id="user-1",
        client_id="user01-phone",
        transcript_versions=[],
        active_transcript_version=None,
    )


def _parent() -> Conversation:
    parent = _blank("parent-1")
    parent.add_transcript_version(
        version_id="asr",
        transcript="hello there general",
        segments=[
            _segment(0.0, 10.0, "", "hello there"),
            _segment(60.0, 70.0, "", "general"),
        ],
        provider="deepgram",
        set_as_active=True,
    )
    parent.add_transcript_version(
        version_id="speaker",
        transcript="hello there general",
        segments=[
            _segment(0.0, 10.0, "alex", "hello there"),
            _segment(60.0, 70.0, "blair", "general"),
        ],
        provider="deepgram",
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "asr",
        },
        set_as_active=True,
    )
    return parent


def _child() -> Conversation:
    return _blank("child-1")


def test_child_receives_every_version_and_keeps_the_chain():
    child = _child()

    active_id = _slice_versions_onto_child(child, _parent(), 0.0, 30.0)

    assert len(child.transcript_versions) == 2
    asr_slice, speaker_slice = child.transcript_versions
    assert active_id == speaker_slice.version_id
    assert child.active_transcript_version == speaker_slice.version_id
    # The speaker slice points at the child's own ASR slice, so a rebuild walking
    # back from active finds real ASR rather than stopping on diarized text.
    assert speaker_slice.metadata["source_version_id"] == asr_slice.version_id
    assert speaker_slice.metadata["source_conversation_id"] == "child-1"
    assert speaker_slice.metadata["reprocessing_type"] == "speaker_diarization"
    assert speaker_slice.metadata["origin_version_id"] == "speaker"
    # Only the in-range segment survives, on both versions alike.
    assert [segment.text for segment in asr_slice.segments] == ["hello there"]
    assert [segment.speaker for segment in speaker_slice.segments] == ["alex"]


def test_active_falls_back_when_the_active_version_slices_to_nothing():
    parent = _parent()
    # A version covering only the tail of the parent leaves this range empty.
    parent.add_transcript_version(
        version_id="tail-only",
        transcript="general",
        segments=[_segment(60.0, 70.0, "blair", "general")],
        metadata={"source_version_id": "speaker"},
        set_as_active=True,
    )
    child = _child()

    active_id = _slice_versions_onto_child(child, parent, 0.0, 30.0)

    assert active_id is not None
    assert active_id == child.transcript_versions[-1].version_id
    assert len(child.transcript_versions) == 2


def test_a_range_with_no_speech_produces_no_versions():
    child = _child()

    active_id = _slice_versions_onto_child(child, _parent(), 20.0, 40.0)

    assert active_id is None
    assert child.transcript_versions == []


def test_a_cut_inside_a_speech_segment_moves_to_its_nearest_edge():
    segments = [
        _segment(0.0, 10.0, "alex", "hello there"),
        _segment(12.0, 40.0, "blair", "a long stretch of talking"),
    ]

    adjusted, moved = _avoid_speech_segments([35.0], segments)

    assert adjusted == [40.0]
    assert moved == [{"requested": 35.0, "moved_to": 40.0, "segment": [12.0, 40.0]}]


def test_a_cut_in_a_gap_is_left_alone():
    segments = [
        _segment(0.0, 10.0, "alex", "hello there"),
        _segment(60.0, 70.0, "blair", "general"),
    ]

    adjusted, moved = _avoid_speech_segments([30.0], segments)

    assert adjusted == [30.0]
    assert moved == []


def test_only_speech_blocks_a_cut():
    laughter = Conversation.SpeakerSegment(
        start=10.0,
        end=20.0,
        speaker="",
        text="[laughter]",
        segment_type=Conversation.SegmentType.EVENT,
    )

    adjusted, moved = _avoid_speech_segments([15.0], [laughter])

    # An event segment carries no words to lose, so cutting through it is fine.
    assert adjusted == [15.0]
    assert moved == []


def test_asr_source_walks_back_through_diarization():
    conversation = _parent()  # asr -> speaker, speaker active

    found = _asr_source_version(conversation)

    assert found is not None
    assert found.version_id == "asr"


def test_asr_source_of_an_undiarized_conversation_is_its_active_version():
    conversation = _blank("plain-1")
    conversation.add_transcript_version(
        version_id="asr-only",
        transcript="hello",
        segments=[_segment(0.0, 5.0, "", "hello")],
        set_as_active=True,
    )

    found = _asr_source_version(conversation)

    assert found is not None
    assert found.version_id == "asr-only"


def test_asr_source_stops_when_the_chain_points_outside_the_conversation():
    conversation = _blank("orphan-1")
    conversation.add_transcript_version(
        version_id="speaker-slice",
        transcript="hello",
        segments=[_segment(0.0, 5.0, "alex", "hello")],
        metadata={
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "lives-on-a-dead-parent",
        },
        set_as_active=True,
    )

    found = _asr_source_version(conversation)

    # Nothing better exists here, so the walk yields what it has rather than None.
    assert found is not None
    assert found.version_id == "speaker-slice"
