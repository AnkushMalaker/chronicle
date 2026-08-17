import asyncio
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


def test_disabled_speaker_service_is_not_counted_as_success():
    completed, error = MODULE._speaker_result_status(
        {
            "success": True,
            "speaker_recognition_enabled": False,
            "processing_time_seconds": 0,
        }
    )

    assert not completed
    assert error == "Speaker recognition is disabled"


def test_speaker_source_walks_back_to_base_asr_version():
    document = {
        "active_transcript_version": "pyannote-2",
        "transcript_versions": [
            {
                "version_id": "smallest-base",
                "transcript": "base transcript",
                "metadata": {},
            },
            {
                "version_id": "pyannote-1",
                "transcript": "first speaker projection",
                "metadata": {
                    "reprocessing_type": "speaker_diarization",
                    "source_version_id": "smallest-base",
                },
            },
            {
                "version_id": "pyannote-2",
                "transcript": "second speaker projection",
                "metadata": {
                    "reprocessing_type": "speaker_diarization",
                    "source_version_id": "pyannote-1",
                },
            },
        ],
    }

    source = MODULE._speaker_source_version(document)

    assert source["version_id"] == "smallest-base"


def test_select_can_bound_a_short_recording_pass_by_duration():
    class EmptyCursor:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Conversations:
        query = None

        def find(self, query, projection):
            self.query = query
            return EmptyCursor()

    class Database:
        conversations = Conversations()

        def __getitem__(self, name):
            assert name == "conversations"
            return self.conversations

    database = Database()

    asyncio.run(
        MODULE._select(
            database,
            ids=[],
            skip_speaker_since=None,
            min_duration=0,
            max_duration=1200,
        )
    )

    assert database.conversations.query["audio_total_duration"] == {
        "$gt": 0,
        "$lte": 1200,
    }


def test_projection_metrics_detect_duplicate_word_ownership_and_overlap():
    source = {
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.5},
            {"word": "there", "start": 0.5, "end": 1.0},
        ]
    }
    projection = {
        "words": source["words"],
        "segments": [
            {
                "start": 0.0,
                "end": 0.8,
                "words": [source["words"][0], source["words"][1]],
            },
            {
                "start": 0.7,
                "end": 1.0,
                "words": [source["words"][1]],
            },
        ],
    }

    metrics = MODULE._projection_metrics(source, projection, duration=1.0)

    assert metrics["duplicate_word_occurrences"] == 1
    assert metrics["missing_words"] == 0
    assert metrics["overlapping_segments"] == 1
    assert metrics["invalid_segment_bounds"] == 0


def test_projection_metrics_preserve_unassigned_asr_words_without_duplicates():
    source = {
        "words": [
            {"word": "speech", "start": 0.0, "end": 0.5},
            {"word": "hallucination", "start": 0.7, "end": 1.0},
        ]
    }
    projection = {
        "words": source["words"],
        "segments": [
            {
                "start": 0.0,
                "end": 0.5,
                "words": [source["words"][0]],
            }
        ],
    }

    metrics = MODULE._projection_metrics(source, projection, duration=1.0)

    assert metrics["duplicate_word_occurrences"] == 0
    assert metrics["missing_words"] == 1
    assert metrics["overlapping_segments"] == 0
    assert metrics["words_preserved"] is True


def test_projection_metrics_accept_exclusive_point_event_boundary():
    projection = {
        "words": [],
        "segments": [
            {"start": 0.0, "end": 5.0, "segment_type": "speech"},
            {"start": 5.0, "end": 5.0, "segment_type": "event"},
            {"start": 5.0, "end": 10.0, "segment_type": "speech"},
        ],
    }

    metrics = MODULE._projection_metrics({"words": []}, projection, duration=10.0)

    assert metrics["overlapping_segments"] == 0
    assert metrics["invalid_segment_bounds"] == 0


def test_validation_accepts_explicit_word_timeline_fallback_after_empty_pyannote():
    word = {"word": "hello", "start": 0.5, "end": 1.0}
    source = {
        "version_id": "raw",
        "transcript": "hello",
        "provider": "smallest",
        "model": "smallest",
        "words": [word],
        "segments": [],
        "metadata": {"transcript_artifact_ids": ["transcript-1"]},
    }
    projection = {
        **source,
        "version_id": "fallback",
        "segments": [
            {
                "start": 0.5,
                "end": 1.0,
                "text": "hello",
                "speaker": "Unknown Speaker 1",
                "segment_type": "speech",
                "words": [word],
            }
        ],
        "diarization_source": "word_timeline_fallback",
        "metadata": {
            "reprocessing_type": "speaker_diarization",
            "source_version_id": "raw",
            "diarization_artifact_id": "diarization-1",
            "transcript_artifact_ids": ["transcript-1"],
            "diarization_fallback": {
                "mode": "word_timeline",
                "reason": "pyannote_empty",
            },
        },
    }
    conversation = {
        "conversation_id": "conversation-1",
        "audio_total_duration": 2.0,
        "active_transcript_version": "fallback",
        "transcript_versions": [source, projection],
    }
    artifact = {
        "artifact_id": "diarization-1",
        "provider": "word_timeline_fallback",
        "turns": [{"start": 0.5, "end": 1.0, "speaker": "Unknown Speaker 1"}],
        "configuration": {
            "requested_source": "pyannote",
            "ran_pyannote_segmentation": True,
            "pyannote_returned_turns": False,
            "fallback_mode": "word_timeline",
            "neural_window_ceiling_seconds": 1200,
            "min_duration": 0,
            "min_duration_off": 0,
            "min_speakers": None,
            "max_speakers": None,
        },
    }
    revision = {
        "retry_key": "speaker-projection:conversation-1:fallback",
        "diarization_artifact_ids": ["diarization-1"],
        "transcript_artifact_ids": ["transcript-1"],
    }

    class Collection:
        def __init__(self, document):
            self.document = document

        async def find_one(self, _query, projection=None):
            return self.document

    class Database:
        collections = {
            "conversations": Collection(conversation),
            "diarization_artifacts": Collection(artifact),
            "conversation_transcript_revisions": Collection(revision),
        }

        def __getitem__(self, name):
            return self.collections[name]

    stats, issues = asyncio.run(
        MODULE._validate_targets(
            Database(),
            [{"conversation_id": "conversation-1", "source_version_id": "raw"}],
        )
    )

    assert issues == []
    assert stats["word_timeline_fallbacks"] == 1
    assert stats["validated"] == 1


def test_validation_accepts_initial_in_place_projection_with_raw_revision():
    word = {"word": "hello", "start": 0.0, "end": 1.0}
    conversation = {
        "conversation_id": "conversation-1",
        "audio_total_duration": 1.0,
        "active_transcript_version": "ingest-version",
        "active_transcript_revision_id": "speaker-revision",
        "transcript_versions": [
            {
                "version_id": "ingest-version",
                "transcript": "hello",
                "provider": "smallest",
                "model": "smallest",
                "words": [word],
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "hello",
                        "speaker": "Alice",
                        "identified_as": "Alice",
                        "segment_type": "speech",
                        "words": [word],
                    }
                ],
                "diarization_source": "pyannote",
                "metadata": {
                    "speaker_recognition": {"enabled": True},
                    "diarization_artifact_id": "diarization-1",
                    "transcript_artifact_ids": ["transcript-1"],
                },
            }
        ],
    }
    raw_revision = {
        "retry_key": "transcript-projection:conversation-1:ingest-version",
        "transcript": "hello",
        "provider": "smallest",
        "model": "smallest",
        "words": [word],
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hello",
                "speaker": "Speaker 0",
                "segment_type": "speech",
                "words": [],
            }
        ],
        "diarization_source": "provider",
        "metadata": {},
        "transcript_artifact_ids": ["transcript-1"],
        "diarization_artifact_ids": [],
    }
    speaker_revision = {
        "revision_id": "speaker-revision",
        "retry_key": "speaker-projection:conversation-1:ingest-version",
        "transcript_artifact_ids": ["transcript-1"],
        "diarization_artifact_ids": ["diarization-1"],
    }
    artifact = {
        "artifact_id": "diarization-1",
        "provider": "pyannote",
        "turns": [{"start": 0.0, "end": 1.0, "speaker": "Alice"}],
        "configuration": {
            "requested_source": "pyannote",
            "ran_pyannote_segmentation": True,
            "pyannote_returned_turns": True,
            "fallback_mode": None,
            "neural_window_ceiling_seconds": 1200,
            "min_duration": 0,
            "min_duration_off": 0,
            "min_speakers": None,
            "max_speakers": None,
        },
    }

    class SingletonCollection:
        def __init__(self, document):
            self.document = document

        async def find_one(self, _query, projection=None):
            return self.document

    class RevisionCollection:
        async def find_one(self, query, projection=None):
            if str(query.get("retry_key", "")).startswith("transcript-projection:"):
                return raw_revision
            return speaker_revision

    class Database:
        collections = {
            "conversations": SingletonCollection(conversation),
            "diarization_artifacts": SingletonCollection(artifact),
            "conversation_transcript_revisions": RevisionCollection(),
        }

        def __getitem__(self, name):
            return self.collections[name]

    stats, issues = asyncio.run(
        MODULE._validate_targets(
            Database(),
            [
                {
                    "conversation_id": "conversation-1",
                    "source_version_id": "ingest-version",
                }
            ],
        )
    )

    assert issues == []
    assert stats["in_place_projections"] == 1
    assert stats["validated"] == 1


def test_identity_metrics_distinguish_recognized_names_from_unknown_clusters():
    totals, visible_names, automatic_names, visible_without_model_id = (
        MODULE._identity_metrics(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "hello",
                        "speaker": "Alice",
                        "identified_as": "Alice",
                        "segment_type": "speech",
                    },
                    {
                        "start": 2.0,
                        "end": 3.5,
                        "text": "there",
                        "speaker": "Unknown Speaker 2",
                        "segment_type": "speech",
                    },
                    {
                        "start": 3.5,
                        "end": 4.0,
                        "text": "human correction",
                        "speaker": "Bob",
                        "identified_as": None,
                        "segment_type": "speech",
                    },
                    {
                        "start": 4.0,
                        "end": 4.5,
                        "text": "[Noise]",
                        "speaker": "Noise",
                        "segment_type": "event",
                    },
                ]
            }
        )
    )

    assert totals == {
        "named_segments": 2,
        "named_duration_ms": 2500,
        "automatically_identified_segments": 1,
        "automatically_identified_duration_ms": 2000,
        "visible_named_without_model_id_segments": 1,
        "visible_named_without_model_id_duration_ms": 500,
        "unknown_segments": 1,
        "unknown_duration_ms": 1500,
    }
    assert visible_names == {"Alice": 1, "Bob": 1}
    assert automatic_names == {"Alice": 1}
    assert visible_without_model_id == {"Bob": 1}
