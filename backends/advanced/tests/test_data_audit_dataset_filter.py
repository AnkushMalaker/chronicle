"""Dataset-scoping regressions for the Data Audit listing."""

from types import SimpleNamespace

import pytest

from advanced_omi_backend.controllers import data_audit_controller
from advanced_omi_backend.models.conversation import Conversation


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args):
        return self

    def limit(self, *_args):
        return self

    async def to_list(self, *, length):
        return self.docs[:length]


class _Collection:
    def __init__(self, listing_docs=None):
        self.queries = []
        self.listing_docs = listing_docs or []

    def find(self, query, projection):
        self.queries.append((query, projection))
        call = len(self.queries)
        if call == 2:
            return _Cursor(
                [
                    {"external_source_id": "dataset-new:clip-2"},
                    {"external_source_id": "dataset-new:clip-1"},
                    {"external_source_id": "dataset-old:clip-1"},
                ]
            )
        if call == 1:
            return _Cursor(self.listing_docs)
        return _Cursor([])


def _conversation(conversation_id, labels):
    return {
        "conversation_id": conversation_id,
        "user_id": "user-1",
        "audio_chunks_count": 1,
        "audio_total_duration": 10.0,
        "active_transcript_version": "active",
        "transcript_versions": [
            {
                "version_id": "active",
                "segments": [
                    {
                        "start": index,
                        "end": index + 1,
                        "speaker": label,
                        "segment_type": "speech",
                    }
                    for index, label in enumerate(labels)
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_list_for_audit_scopes_and_lists_annotation_datasets(monkeypatch):
    collection = _Collection()
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)
    user = SimpleNamespace(is_superuser=False, user_id="user-1")

    result = await data_audit_controller.list_for_audit(
        user,
        dataset_id="dataset.+(selected)",
    )

    listing_query = collection.queries[0][0]
    assert listing_query["external_source_type"] == "annotation_dataset"
    assert listing_query["external_source_id"] == {
        "$regex": r"^dataset\.\+\(selected\):"
    }
    assert result["datasets"] == ["dataset-new", "dataset-old"]


@pytest.mark.asyncio
async def test_unknown_placeholders_are_one_filter_facet_not_global_identities(
    monkeypatch,
):
    collection = _Collection(
        [
            _conversation("one", ["Unknown Speaker 1", "Ankush"]),
            _conversation("two", ["unknown_speaker_1", "Unknown Speaker 7"]),
            _conversation("three", ["Daksh"]),
        ]
    )
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)
    user = SimpleNamespace(is_superuser=False, user_id="user-1")

    result = await data_audit_controller.list_for_audit(user)

    assert result["speakers"] == ["Ankush", "Daksh"]
    assert result["has_unknown_speakers"] is True
    assert result["conversations"][0]["speakers"] == ["Ankush", "Unknown Speaker 1"]


@pytest.mark.asyncio
async def test_unknown_filter_includes_all_local_placeholders_and_combines_with_names(
    monkeypatch,
):
    collection = _Collection(
        [
            _conversation("unknown", ["Unknown Speaker 3"]),
            _conversation("named", ["Ankush"]),
            _conversation("other", ["Daksh"]),
        ]
    )
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)
    user = SimpleNamespace(is_superuser=False, user_id="user-1")

    result = await data_audit_controller.list_for_audit(
        user, include_speakers=["Ankush"], unknown_speakers="include"
    )

    assert {row["conversation_id"] for row in result["conversations"]} == {
        "unknown",
        "named",
    }


@pytest.mark.asyncio
async def test_unknown_filter_excludes_every_placeholder_variant(monkeypatch):
    collection = _Collection(
        [
            _conversation("one", ["Unknown"]),
            _conversation("two", ["Unknown Speaker 9"]),
            _conversation("named", ["Ankush"]),
        ]
    )
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)
    user = SimpleNamespace(is_superuser=False, user_id="user-1")

    result = await data_audit_controller.list_for_audit(
        user, unknown_speakers="exclude"
    )

    assert [row["conversation_id"] for row in result["conversations"]] == ["named"]
