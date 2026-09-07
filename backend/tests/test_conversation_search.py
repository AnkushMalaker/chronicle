from backend.controllers.conversation_controller import (
    _LIST_PROJECTION,
    _raw_doc_to_list_dict,
    _search_fields,
    _search_query_stages,
)


def test_everything_search_includes_all_categories():
    fields = _search_fields(["id", "title", "summary", "speakers"])

    assert "conversation_id" in fields
    assert "title" in fields
    assert "summary" in fields
    assert "detailed_summary" in fields
    assert "_search_active_version.segments.speaker" in fields
    assert "_search_active_version.segments.identified_as" not in fields


def test_search_categories_are_independent():
    assert _search_fields(["id"]) == ["conversation_id"]
    assert _search_fields(["title"]) == ["title"]
    assert _search_fields(["summary"]) == ["summary", "detailed_summary"]
    assert _search_fields(["speakers"]) == ["_search_active_version.segments.speaker"]


def test_conversation_id_search_matches_literal_fragments():
    stages = _search_query_stages("abc.123", _search_fields(["id"]))

    assert stages == [
        {
            "$match": {
                "$or": [
                    {
                        "conversation_id": {
                            "$regex": r"abc\.123",
                            "$options": "i",
                        }
                    }
                ]
            }
        }
    ]


def test_speaker_search_resolves_only_the_active_transcript_version():
    fields = _search_fields(["speakers"])
    stages = _search_query_stages("unshull", fields)

    assert stages[0]["$set"]["_search_active_version"]["$arrayElemAt"][0]["$filter"][
        "cond"
    ] == {"$eq": ["$$version.version_id", "$active_transcript_version"]}
    clauses = stages[1]["$match"]["$or"]
    assert clauses == [
        {
            "_search_active_version.segments.speaker": {
                "$regex": "unshull",
                "$options": "i",
            }
        }
    ]


def test_list_projection_excludes_full_transcript_segments():
    assert _LIST_PROJECTION["transcript_versions.segments.speaker"] == 1
    assert "transcript_versions.segments" not in _LIST_PROJECTION
    assert "transcript_versions.segments.text" not in _LIST_PROJECTION
    assert "transcript_versions.segments.words" not in _LIST_PROJECTION
    assert "transcript_versions.transcript" not in _LIST_PROJECTION


def test_list_conversion_uses_lightweight_speaker_projection():
    result = _raw_doc_to_list_dict(
        {
            "active_transcript_version": "version-2",
            "transcript_versions": [
                {"version_id": "version-1", "segments": [{"speaker": "Speaker 0"}]},
                {
                    "version_id": "version-2",
                    "segments": [
                        {"speaker": "Alice"},
                        {"speaker": "Alice"},
                        {"speaker": "Bob"},
                    ],
                },
            ],
        }
    )

    assert result["segment_count"] == 3
    assert result["speakers"] == ["Alice", "Bob"]
    assert result["transcript_version_count"] == 2
    assert result["active_transcript_version_number"] == 2
