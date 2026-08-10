import importlib.util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parents[1] / "src" / "scripts" / "reprocess_speakers_corpus.py"
)
SPEC = importlib.util.spec_from_file_location("reprocess_speakers_corpus", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_speaker_work_excludes_explicit_event_only_segments():
    assert not MODULE._has_speaker_work(
        {
            "words": [{"word": "noise", "start": 0.0, "end": 1.0}],
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "[Noise]",
                    "segment_type": "event",
                }
            ],
        }
    )


def test_speaker_work_accepts_speech_segments_or_unsegmented_words():
    assert MODULE._has_speaker_work(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                    "segment_type": "speech",
                }
            ]
        }
    )
    assert MODULE._has_speaker_work({"segments": [], "words": [{"word": "hello"}]})


def test_speaker_work_recovers_spoken_text_mislabeled_as_event():
    assert MODULE._has_speaker_work(
        {
            "segments": [
                {
                    "start": 1.0,
                    "end": 4.0,
                    "text": "We should decide which base model to use next.",
                    "segment_type": "event",
                }
            ],
            "words": [{"word": "decide", "start": 1.5, "end": 2.0}],
        }
    )
