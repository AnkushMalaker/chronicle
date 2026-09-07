"""Pure helpers for re-timing transcript segments/words across split/merge.

Split children take a time slice of the parent's transcript with timestamps
shifted to start at zero; merged conversations concatenate source transcripts
with cumulative offsets. Segment membership uses the segment midpoint — split
points sit inside long silence gaps, so segments never straddle them.
"""

from typing import List

from backend.models.conversation import Conversation


def slice_words(
    words: List[Conversation.Word], t0: float, t1: float
) -> List[Conversation.Word]:
    """Words starting within [t0, t1), shifted by -t0."""
    return [
        word.model_copy(
            update={
                "start": round(word.start - t0, 3),
                "end": round(min(word.end, t1) - t0, 3),
            }
        )
        for word in words
        if t0 <= word.start < t1
    ]


def slice_segments(
    segments: List[Conversation.SpeakerSegment], t0: float, t1: float
) -> List[Conversation.SpeakerSegment]:
    """Segments whose midpoint lies within [t0, t1), clamped and shifted by -t0."""
    sliced = []
    for seg in segments:
        midpoint = (seg.start + seg.end) / 2
        if not (t0 <= midpoint < t1):
            continue
        sliced.append(
            seg.model_copy(
                update={
                    "start": round(max(seg.start, t0) - t0, 3),
                    "end": round(min(seg.end, t1) - t0, 3),
                    "words": slice_words(seg.words or [], t0, t1),
                }
            )
        )
    return sliced


def shift_words(
    words: List[Conversation.Word], offset: float
) -> List[Conversation.Word]:
    return [
        word.model_copy(
            update={
                "start": round(word.start + offset, 3),
                "end": round(word.end + offset, 3),
            }
        )
        for word in words
    ]


def shift_segments(
    segments: List[Conversation.SpeakerSegment], offset: float
) -> List[Conversation.SpeakerSegment]:
    return [
        seg.model_copy(
            update={
                "start": round(seg.start + offset, 3),
                "end": round(seg.end + offset, 3),
                "words": shift_words(seg.words or [], offset),
            }
        )
        for seg in segments
    ]


def build_transcript_text(segments: List[Conversation.SpeakerSegment]) -> str:
    """Full transcript text from speech segments (skips event/note markers)."""
    return " ".join(
        seg.text.strip()
        for seg in segments
        if seg.text and seg.segment_type == Conversation.SegmentType.SPEECH
    ).strip()
