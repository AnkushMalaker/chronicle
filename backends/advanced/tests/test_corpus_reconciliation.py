from advanced_omi_backend.services.corpus_reconciliation import (
    ReconciliationRecord,
    build_manifest,
    conservative_transcript_match,
    encoded_identity,
    pcm_identity,
)


def record(conversation_id: str, **values: object) -> ReconciliationRecord:
    return ReconciliationRecord(
        conversation_id=conversation_id,
        user_id="user",
        **values,
    )


def test_pcm_identity_includes_format_and_chunk_hash_is_integrity_only() -> None:
    data = b"\x00\x01" * 100
    assert pcm_identity(data, 16000, 1) == pcm_identity(data, 16000, 1)
    assert pcm_identity(data, 16000, 1) != pcm_identity(data, 8000, 1)
    assert encoded_identity(data) != pcm_identity(data, 16000, 1)


def test_exact_pcm_prefers_normal_audio_record_and_retires_derivatives() -> None:
    manifest = build_manifest(
        [
            record(
                "normal",
                transcript="My wife is Anushpa",
                pcm_sha256="same",
                has_audio=True,
            ),
            record(
                "old-copy",
                transcript="My wife is Anushpa",
                pcm_sha256="same",
                has_audio=True,
                deleted=True,
            ),
            record(
                "mined",
                transcript="My wife is Anushpa",
                pcm_sha256="same",
                has_audio=True,
                data_purpose="annotation",
            ),
        ]
    )
    assert manifest["activation_allowed"]
    assert manifest["canonical_by_source"] == {
        "mined": "normal",
        "normal": "normal",
        "old-copy": "normal",
    }


def test_transcript_only_exact_alias_joins_audio_canonical() -> None:
    manifest = build_manifest(
        [
            record("audio", transcript="Hello, my wife is Anushpa.", has_audio=True),
            record("alias", transcript="hello my wife is anushpa", has_audio=False),
        ]
    )
    assert manifest["canonical_by_source"]["alias"] == "audio"


def test_ambiguous_transcript_alias_blocks_activation() -> None:
    transcript = "one two three four five six seven eight nine ten"
    manifest = build_manifest(
        [
            record("a", transcript=transcript, has_audio=True),
            record("b", transcript=transcript, has_audio=True),
            record("alias", transcript=transcript),
        ]
    )
    assert not manifest["activation_allowed"]
    assert manifest["blockers"][0]["reason"] == "ambiguous_transcript_alias"


def test_conservative_match_rejects_short_or_materially_different_text() -> None:
    matched, _ = conservative_transcript_match(
        "my wife is anushpa", "my wife is anushpa"
    )
    # Distinctive five-gram coverage intentionally prevents short fuzzy aliases.
    assert not matched
    matched, _ = conservative_transcript_match(
        "one two three four five six seven eight nine ten",
        "one two three four five six seven something else ten",
    )
    assert not matched
