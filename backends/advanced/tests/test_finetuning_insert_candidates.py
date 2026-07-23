from types import SimpleNamespace

from advanced_omi_backend.models.annotation import AnnotationType
from advanced_omi_backend.routers.modules.finetuning_routes import (
    _resolve_inserted_segment,
)


def test_processed_speech_insert_resolves_to_active_segment_after_apply_sort():
    segments = [
        SimpleNamespace(start=8.0, end=11.0, speaker="ankush", text="before"),
        SimpleNamespace(
            start=12.367411915438822,
            end=13.597392696989111,
            speaker="jit bahadur",
            text="thoda thoda dalna padega bhaiya",
        ),
        SimpleNamespace(start=19.0, end=22.0, speaker="ankush", text="after"),
    ]
    annotation = SimpleNamespace(
        annotation_type=AnnotationType.INSERT,
        insert_after_index=2,
        insert_segment_type="speech",
        insert_speaker="jit bahadur",
        insert_text="thoda thoda dalna padega bhaiya",
        insert_start=12.367411915438822,
        insert_end=13.597392696989111,
        processed=True,
        processed_by="apply",
    )

    assert _resolve_inserted_segment(segments, annotation) == 1


def test_event_insert_is_not_an_enrollment_candidate():
    segment = SimpleNamespace(
        start=12.0, end=16.0, speaker="jit bahadur", text="[laughter]"
    )
    annotation = SimpleNamespace(
        insert_segment_type="event",
        insert_speaker="jit bahadur",
        insert_text="[laughter]",
        insert_start=12.0,
        insert_end=16.0,
    )

    assert _resolve_inserted_segment([segment], annotation) is None
