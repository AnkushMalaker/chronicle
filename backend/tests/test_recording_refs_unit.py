import pytest

from backend.models.conversation import Conversation
from backend.services.timeline.recording_refs import resolve_live_recordings


class _Cursor:
    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return self.documents


class _ConversationCollection:
    def __init__(self, documents):
        self.documents = {
            document["conversation_id"]: document for document in documents
        }
        self.find_batches = []

    def find(self, query, projection):
        ids = set(query["conversation_id"]["$in"])
        self.find_batches.append(ids)
        return _Cursor([self.documents[item] for item in ids if item in self.documents])


@pytest.mark.asyncio
async def test_lineage_resolution_batches_each_depth_without_materializing_models(
    monkeypatch,
):
    collection = _ConversationCollection(
        [
            {
                "conversation_id": "source",
                "deleted": True,
                "derived_into": ["middle-a", "middle-b"],
                "client_id": "screenpipe",
            },
            {
                "conversation_id": "middle-a",
                "deleted": True,
                "derived_into": ["live"],
                "client_id": "screenpipe",
            },
            {
                "conversation_id": "middle-b",
                "deleted": True,
                "derived_into": ["live"],
                "client_id": "screenpipe",
            },
            {
                "conversation_id": "live",
                "deleted": False,
                "derived_into": [],
                "client_id": "screenpipe",
            },
        ]
    )
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)

    resolved = await resolve_live_recordings(["source"])

    assert resolved == {"live"}
    assert collection.find_batches == [
        {"source"},
        {"middle-a", "middle-b"},
        {"live"},
    ]
