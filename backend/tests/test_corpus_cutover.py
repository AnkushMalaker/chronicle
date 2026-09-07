from datetime import datetime, timedelta, timezone

from bson import ObjectId

from backend.services.corpus_cutover import (
    FORBIDDEN_CHUNK_FIELDS,
    build_conversation_document,
    build_processing_corpus,
    classify_collections,
    convert_capture_chunk,
    plan_capture_corpus,
    plan_conversation_audio,
    should_materialize_conversation,
    transform_device_input_item,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def conversation(conversation_id: str, *, client_id: str = "user-device", **extra):
    return {
        "_id": ObjectId(),
        "conversation_id": conversation_id,
        "user_id": "user-1",
        "client_id": client_id,
        "created_at": NOW,
        "deleted": False,
        "data_purpose": None,
        "transcript_versions": [],
        "active_transcript_version": None,
        **extra,
    }


def chunk(
    conversation_id: str,
    index: int,
    *,
    captured_at: datetime | None,
    stream: str | None = "stream-1",
    first_message: int | None = None,
    last_message: int | None = None,
    duration: float = 10.0,
    audio: bytes = b"opus",
    deleted: bool = False,
):
    document = {
        "_id": ObjectId(),
        "conversation_id": conversation_id,
        "chunk_index": index,
        "start_time": index * 10.0,
        "end_time": index * 10.0 + duration,
        "duration": duration,
        "captured_at": captured_at,
        "created_at": NOW + timedelta(seconds=index * 10),
        "source_stream": stream,
        "source_first_message_id": (
            f"{first_message}-0" if first_message is not None else None
        ),
        "source_last_message_id": (
            f"{last_message}-0" if last_message is not None else None
        ),
        "source_message_ids": [],
        "sample_rate": 16000,
        "channels": 1,
        "original_size": int(duration * 32000),
        "compressed_size": len(audio),
        "audio_data": audio,
        "vad": None,
        "deleted": deleted,
        "deleted_at": NOW if deleted else None,
    }
    return document


def test_collection_classification_refuses_unknown_data():
    copied, transformed, regenerated, unknown = classify_collections(
        ["users", "audio_chunks", "timeline_days", "new_durable_evidence"]
    )

    assert copied == {"users"}
    assert transformed == {"audio_chunks"}
    assert regenerated == {"timeline_days"}
    assert unknown == {"new_durable_evidence"}


def test_collection_classification_preserves_reviews_and_discards_pairing_codes():
    copied, transformed, regenerated, unknown = classify_collections(
        [
            "background_clips",
            "background_foreground_clips",
            "background_suppressions",
            "device_input_pairing_codes",
        ]
    )

    assert copied == {"background_clips", "background_foreground_clips"}
    assert transformed == {"background_suppressions"}
    assert regenerated == {"device_input_pairing_codes"}
    assert unknown == set()


def test_capture_sessions_are_global_across_conversations():
    first = conversation("conversation-a")
    second = conversation("conversation-b")
    chunks = [
        chunk(
            "conversation-a",
            0,
            captured_at=NOW,
            first_message=1000,
            last_message=1900,
        ),
        chunk(
            "conversation-b",
            0,
            captured_at=NOW + timedelta(seconds=10),
            first_message=2000,
            last_message=2900,
        ),
    ]

    plan = plan_capture_corpus(
        {"conversation-a": first, "conversation-b": second}, chunks
    )

    assert len(plan.capture_sessions) == 1
    assignments = [plan.assignments[str(item["_id"])] for item in chunks]
    assert assignments[0].capture_session_id == assignments[1].capture_session_id
    assert [item.sequence for item in assignments] == [0, 1]
    assert plan.capture_sessions[0]["source_stream"] == "stream-1"

    repeated = plan_capture_corpus(
        {"conversation-a": first, "conversation-b": second}, chunks
    )
    assert repeated.capture_sessions == plan.capture_sessions


def test_one_conversation_can_claim_multiple_capture_sessions():
    source = conversation("conversation-a")
    chunks = [
        chunk("conversation-a", 0, captured_at=NOW, stream="stream-1"),
        chunk(
            "conversation-a",
            1,
            captured_at=NOW + timedelta(seconds=10),
            stream="stream-2",
        ),
    ]
    corpus = plan_capture_corpus({"conversation-a": source}, chunks)
    claim = plan_conversation_audio(source, chunks, corpus.assignments)

    assert len(corpus.capture_sessions) == 2
    assert len(claim.audio_ranges) == 1
    assert len(claim.audio_ranges[0].capture_session_ids) == 2


def test_duplicate_operational_index_is_excluded_only_from_claim():
    source = conversation("conversation-a")
    before = chunk(
        "conversation-a",
        0,
        captured_at=NOW,
        first_message=0,
        last_message=1000,
        audio=b"before",
    )
    overlapping = chunk(
        "conversation-a",
        1,
        captured_at=NOW + timedelta(seconds=8),
        first_message=800,
        last_message=1800,
        audio=b"overlap",
    )
    coherent = chunk(
        "conversation-a",
        1,
        captured_at=NOW + timedelta(seconds=10),
        first_message=1100,
        last_message=2000,
        audio=b"coherent",
    )
    after = chunk(
        "conversation-a",
        2,
        captured_at=NOW + timedelta(seconds=20),
        first_message=2100,
        last_message=3000,
        audio=b"after",
    )
    chunks = [before, overlapping, coherent, after]
    corpus = plan_capture_corpus({"conversation-a": source}, chunks)
    claim = plan_conversation_audio(source, chunks, corpus.assignments)

    # Every blob moves into capture ownership; only the ambiguous overlap is absent
    # from the semantic playback claim.
    assert {item["_id"] for item in claim.chunks} == {item["_id"] for item in chunks}
    assert claim.claimed_chunk_count == 3
    assert [item["source_chunk_id"] for item in claim.quarantined] == [
        str(overlapping["_id"])
    ]
    assert str(overlapping["_id"]) not in claim.audio_ranges[0].chunk_ids
    assert str(coherent["_id"]) in claim.audio_ranges[0].chunk_ids


def test_unanchored_upload_uses_unknown_time_basis_without_dropping_audio():
    source = conversation("upload", client_id="user-upload")
    chunks = [
        chunk("upload", 0, captured_at=None, stream=None),
        chunk("upload", 1, captured_at=None, stream=None),
    ]
    corpus = plan_capture_corpus({"upload": source}, chunks)
    claim = plan_conversation_audio(source, chunks, corpus.assignments)

    assert [item.captured_at for item in corpus.assignments.values()] == [
        NOW,
        NOW + timedelta(seconds=10),
    ]
    assert corpus.capture_sessions[0]["time_basis"] == "unknown"
    assert claim.audio_ranges[0].time_basis == "unknown"


def test_missing_time_between_real_anchors_is_derived_as_recorded():
    source = conversation("conversation-a")
    chunks = [
        chunk("conversation-a", 0, captured_at=NOW),
        chunk("conversation-a", 1, captured_at=None),
        chunk("conversation-a", 2, captured_at=NOW + timedelta(seconds=20)),
    ]
    corpus = plan_capture_corpus({"conversation-a": source}, chunks)

    middle = corpus.assignments[str(chunks[1]["_id"])]
    assert middle.captured_at == NOW + timedelta(seconds=10)
    assert middle.time_basis == "received"
    assert corpus.capture_sessions[0]["time_basis"] == "received"


def test_chunk_conversion_preserves_bytes_and_removes_container_coordinates():
    source_conversation = conversation("conversation-a")
    source_chunk = chunk("conversation-a", 0, captured_at=NOW, audio=b"exact-opus")
    corpus = plan_capture_corpus(
        {"conversation-a": source_conversation}, [source_chunk]
    )
    converted = convert_capture_chunk(
        source_chunk, corpus.assignments[str(source_chunk["_id"])]
    )

    assert converted["_id"] == source_chunk["_id"]
    assert converted["audio_data"] is source_chunk["audio_data"]
    assert not FORBIDDEN_CHUNK_FIELDS.intersection(converted)
    assert converted["deleted"] is False


def test_disabled_raw_audio_is_preserved_but_never_claimed():
    source = conversation("conversation-a")
    enabled = chunk("conversation-a", 0, captured_at=NOW)
    disabled = chunk(
        "conversation-a",
        1,
        captured_at=NOW + timedelta(seconds=10),
        deleted=True,
    )
    corpus = plan_capture_corpus({"conversation-a": source}, [enabled, disabled])
    claim = plan_conversation_audio(source, [enabled, disabled], corpus.assignments)
    converted = convert_capture_chunk(
        disabled, corpus.assignments[str(disabled["_id"])]
    )

    assert converted["deleted"] is True
    assert converted["deleted_at"] == NOW
    assert converted["deletion_reason"] == "pre_cutover_disabled"
    assert claim.claimed_chunk_count == 1
    assert str(disabled["_id"]) not in claim.audio_ranges[0].chunk_ids
    assert [item["reason"] for item in claim.quarantined] == ["pre_cutover_disabled"]


def test_conversation_document_has_claim_count_not_raw_duplicate_count():
    source = conversation(
        "conversation-a",
        always_persist=True,
        source_session_id="old-session",
    )
    chunks = [
        chunk("conversation-a", 0, captured_at=NOW, first_message=0, last_message=900),
        chunk(
            "conversation-a",
            1,
            captured_at=NOW + timedelta(seconds=8),
            first_message=800,
            last_message=1800,
        ),
        chunk(
            "conversation-a",
            1,
            captured_at=NOW + timedelta(seconds=10),
            first_message=1000,
            last_message=1900,
        ),
    ]
    corpus = plan_capture_corpus({"conversation-a": source}, chunks)
    plan = plan_conversation_audio(source, chunks, corpus.assignments)
    current = build_conversation_document(
        source,
        plan,
        active_revision_id="revision",
        allowed_fields={
            "conversation_id",
            "user_id",
            "client_id",
            "always_persist",
            "source_session_id",
            "audio_ranges",
            "started_at",
            "ended_at",
            "origin",
            "segmentation_key",
            "active_transcript_revision_id",
            "audio_chunks_count",
            "audio_total_duration",
            "audio_compression_ratio",
            "created_at",
        },
    )

    assert current["audio_chunks_count"] == 2
    assert "always_persist" not in current
    assert "source_session_id" not in current
    assert current["active_transcript_revision_id"] == "revision"


def test_processing_cutover_separates_stt_diarization_and_every_revision():
    source = conversation(
        "conversation-a",
        transcript_versions=[
            {
                "version_id": "smallest-v1",
                "provider": "smallest",
                "transcript": "hello",
                "words": [{"word": "hello", "start": 0.5, "end": 10.5}],
                "segments": [
                    {
                        "speaker": "provider-0",
                        "text": "hello",
                        "start": 0.5,
                        "end": 10.5,
                    }
                ],
                "created_at": NOW,
            },
            {
                "version_id": "pyannote-v2",
                "provider": "smallest",
                "transcript": "hello",
                "words": [{"word": "hello", "start": 0.5, "end": 10.0}],
                "segments": [
                    {
                        "speaker": "SPEAKER_00",
                        "identified_as": "Alex",
                        "text": "hello",
                        "start": 0.5,
                        "end": 10.0,
                    },
                    {
                        "speaker": "system",
                        "segment_type": "note",
                        "text": "[merged gap]",
                        "start": 9.0,
                        "end": 9.5,
                    },
                ],
                "diarization_source": "pyannote",
                "metadata": {
                    "reprocessing_type": "speaker_diarization",
                    "source_version_id": "smallest-v1",
                },
                "created_at": NOW + timedelta(seconds=1),
            },
        ],
        active_transcript_version="pyannote-v2",
    )
    source_chunk = chunk("conversation-a", 0, captured_at=NOW)
    corpus = plan_capture_corpus({"conversation-a": source}, [source_chunk])
    claim = plan_conversation_audio(source, [source_chunk], corpus.assignments)

    processing = build_processing_corpus([source], {"conversation-a": claim})

    assert len(processing.transcript_artifacts) == 1
    assert len(processing.diarization_artifacts) == 1
    assert len(processing.revisions_by_conversation["conversation-a"]) == 2
    artifact = processing.transcript_artifacts[0]
    assert artifact["raw_response"]["relative_words"][0]["end"] == 10.5
    assert artifact["words"][0]["audio_spans"][0]["ended_at"] == NOW + timedelta(
        seconds=10
    )
    diarization = processing.diarization_artifacts[0]
    assert diarization["provider"] == "legacy-pyannote-derived"
    assert diarization["turns"][0]["identified_as"] == "Alex"
    assert diarization["turns"][0]["start_seconds"] == 0.5
    assert diarization["turns"][0]["end_seconds"] == 10.0
    assert diarization["turns"][0]["audio_spans"]
    assert len(diarization["turns"]) == 1
    assert diarization["configuration"]["excluded_non_speech_turns"] == 1
    active = processing.active_revision_ids["conversation-a"]
    assert (
        active
        == processing.revisions_by_conversation["conversation-a"][1]["revision_id"]
    )


def test_processing_timestamps_choose_the_correct_side_of_a_capture_gap():
    source = conversation(
        "conversation-a",
        transcript_versions=[
            {
                "version_id": "provider-v1",
                "provider": "smallest",
                "transcript": "before after",
                "words": [
                    {"word": "before", "start": 9.0, "end": 10.0},
                    {"word": "after", "start": 10.0, "end": 11.0},
                ],
                "segments": [],
                "created_at": NOW,
            }
        ],
    )
    chunks = [
        chunk("conversation-a", 0, captured_at=NOW),
        chunk("conversation-a", 1, captured_at=NOW + timedelta(seconds=20)),
    ]
    corpus = plan_capture_corpus({"conversation-a": source}, chunks)
    claim = plan_conversation_audio(source, chunks, corpus.assignments)

    processing = build_processing_corpus([source], {"conversation-a": claim})

    assert len(claim.audio_ranges) == 2
    words = processing.transcript_artifacts[0]["words"]
    assert words[0]["audio_spans"][0]["ended_at"] == NOW + timedelta(seconds=10)
    assert words[1]["audio_spans"][0]["started_at"] == NOW + timedelta(seconds=20)


def test_invalid_provider_timing_is_quarantined_without_losing_revision():
    source = conversation(
        "conversation-a",
        transcript_versions=[
            {
                "version_id": "bad",
                "provider": "smallest",
                "transcript": "hello",
                "words": [{"word": "hello", "start": -1.0, "end": 2.0}],
                "segments": [],
                "metadata": {},
                "created_at": NOW,
            }
        ],
        active_transcript_version="bad",
    )
    source_chunk = chunk("conversation-a", 0, captured_at=NOW)
    corpus = plan_capture_corpus({"conversation-a": source}, [source_chunk])
    claim = plan_conversation_audio(source, [source_chunk], corpus.assignments)

    processing = build_processing_corpus([source], {"conversation-a": claim})

    assert processing.transcript_artifacts == ()
    assert len(processing.revisions_by_conversation["conversation-a"]) == 1
    assert (
        processing.revisions_by_conversation["conversation-a"][0]["words"][0]["start"]
        == -1.0
    )
    assert processing.quarantined[0]["reason"] == "transcript_timing_invalid_range"


def test_only_user_touched_capture_placeholder_survives_as_conversation():
    placeholder = conversation("placeholder", data_purpose="capture_evidence")

    assert should_materialize_conversation(placeholder, set()) is False
    assert should_materialize_conversation(placeholder, {"placeholder"}) is True
    assert (
        should_materialize_conversation(
            {**placeholder, "deleted": True}, {"placeholder"}
        )
        is True
    )


def test_device_input_keeps_screenshot_bytes_but_clears_stale_vault_links():
    source = {
        "_id": ObjectId(),
        "user_id": "user-1",
        "source_id": "screenpipe",
        "kind": "screen_context",
        "source_item_id": "frame-1",
        "captured_at": NOW,
        "media_data": b"jpeg-bytes",
        "conversation_id": "placeholder",
        "related_conversation_ids": ["placeholder", "real"],
        "promoted_path": "Daily/old.png",
        "vault_paths": ["Daily/old.md"],
        "state": "promoted",
    }
    current = transform_device_input_item(
        source,
        {"real"},
        allowed_fields=set(source) | {"vault_paths", "related_conversation_ids"},
    )

    assert current["media_data"] == b"jpeg-bytes"
    assert current["conversation_id"] is None
    assert current["related_conversation_ids"] == ["real"]
    assert current["promoted_path"] is None
    assert current["vault_paths"] == []
    assert current["state"] == "linked"
    assert current["metadata"]["cutover_source_promoted_path"] == "Daily/old.png"


def test_device_input_requires_path_only_media_to_be_recovered_and_verified():
    source = {
        "_id": ObjectId(),
        "user_id": "user-1",
        "source_id": "immich",
        "kind": "immich_memory",
        "source_item_id": "asset-1",
        "captured_at": NOW,
        "media_data": None,
        "content_hash": "21b6568a5351c0fb046c671a32e0fa995f46e9d325d47cf36f59c1e86154d235",
        "promoted_path": "_media/evidence.heic",
        "vault_paths": [],
        "state": "promoted",
    }

    try:
        transform_device_input_item(
            source, set(), allowed_fields=set(source) | {"metadata"}
        )
    except ValueError as error:
        assert "would lose promoted media" in str(error)
    else:
        raise AssertionError("path-only media was silently discarded")

    current = transform_device_input_item(
        source,
        set(),
        allowed_fields=set(source)
        | {"metadata", "media_filename", "media_content_type"},
        recovered_media=b"heic-bytes",
        recovered_media_filename="evidence.heic",
        recovered_media_content_type="image/heic",
    )
    assert current["media_data"] == b"heic-bytes"
    assert (
        current["content_hash"]
        == "21b6568a5351c0fb046c671a32e0fa995f46e9d325d47cf36f59c1e86154d235"
    )
