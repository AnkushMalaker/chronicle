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
    def __init__(self):
        self.queries = []

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
        return _Cursor([])


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
