"""Mutation-boundary checks for new structured vault notes."""

import pytest

from advanced_omi_backend.services.memory.agent.vault_tools import (
    VaultToolError,
    _assert_new_note_schema,
)
from advanced_omi_backend.services.memory.vault_templates import (
    PERSON_TEMPLATE,
    TOPIC_TEMPLATE,
)


@pytest.mark.parametrize(
    ("path", "content", "missing"),
    [
        (
            "People/Alice.md",
            "## About\n- Works on Chronicle.\n",
            "## Conversations",
        ),
        (
            "Topics/Memory Agents.md",
            "## About\n- Agent evaluation.\n\n## Conversations\n",
            "Conversations.base#Topic",
        ),
    ],
)
def test_incomplete_new_spine_note_is_rejected(path, content, missing):
    with pytest.raises(VaultToolError, match=missing):
        _assert_new_note_schema(path, content)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("People/Alice.md", PERSON_TEMPLATE),
        ("Topics/Memory Agents.md", TOPIC_TEMPLATE),
        ("Projects/Chronicle.md", "## About\n- Project note."),
    ],
)
def test_complete_or_organic_new_note_is_accepted(path, content):
    _assert_new_note_schema(path, content)
