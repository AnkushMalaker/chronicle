"""Entry-point regressions for the three summary-bundle workers."""

import httpx
import openai
import pytest

from backend.constants import TITLE_NOT_GENERATED
from backend.models import job as job_model
from backend.utils import conversation_utils
from backend.workers import conversation_jobs


class _Redis:
    async def close(self):
        return None


class _Conversation:
    conversation_id = "conversation-1"
    memory_excluded = False
    data_purpose = "conversation"
    user_id = "user-1"
    transcript = "A useful conversation about rebuilding the memory vault."
    segments = []
    title = "Recording..."
    summary = ""
    detailed_summary = ""
    memory_space_id = None

    async def save(self):
        return self


def _patch_job_runtime(monkeypatch, conversation):
    async def initialize():
        return None

    async def find_one(*args, **kwargs):
        return conversation

    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", initialize)
    monkeypatch.setattr(job_model, "create_async_redis", lambda: _Redis())
    monkeypatch.setattr(
        conversation_jobs.Conversation, "conversation_id", object(), raising=False
    )
    monkeypatch.setattr(conversation_jobs.Conversation, "find_one", find_one)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *args: None)
    monkeypatch.setattr(conversation_jobs, "update_job_meta", lambda **kwargs: None)
    monkeypatch.setattr(conversation_jobs, "set_otel_session", lambda *args: None)
    monkeypatch.setattr(conversation_jobs, "set_span_attrs", lambda **kwargs: None)
    monkeypatch.setattr(conversation_jobs, "set_trace_io", lambda **kwargs: None)


def test_registered_title_and_short_summary_jobs_write_separate_fields(monkeypatch):
    conversation = _Conversation()
    _patch_job_runtime(monkeypatch, conversation)

    async def title(*args, **kwargs):
        return "Memory rebuild"

    async def short_summary(*args, **kwargs):
        return "Discussed rebuilding memory safely."

    monkeypatch.setattr(conversation_jobs, "generate_conversation_title", title)
    monkeypatch.setattr(conversation_jobs, "generate_short_summary", short_summary)

    title_result = conversation_jobs.generate_title_job(conversation.conversation_id)
    summary_result = conversation_jobs.generate_short_summary_job(
        conversation.conversation_id
    )

    assert title_result["title"] == "Memory rebuild"
    assert summary_result["summary"] == "Discussed rebuilding memory safely."
    assert conversation.title == "Memory rebuild"
    assert conversation.summary == "Discussed rebuilding memory safely."


def test_registered_title_job_rejects_the_missing_title_placeholder(monkeypatch):
    conversation = _Conversation()
    _patch_job_runtime(monkeypatch, conversation)

    async def missing_title(*args, **kwargs):
        return TITLE_NOT_GENERATED

    monkeypatch.setattr(conversation_jobs, "generate_conversation_title", missing_title)

    with pytest.raises(RuntimeError, match="missing-title placeholder"):
        conversation_jobs.generate_title_job(conversation.conversation_id)
    assert conversation.title == "Recording..."


def test_memory_excluded_user_visible_conversation_still_gets_a_title(monkeypatch):
    conversation = _Conversation()
    conversation.memory_excluded = True
    conversation.data_purpose = "conversation"
    _patch_job_runtime(monkeypatch, conversation)

    async def title(*args, **kwargs):
        return "Ranked strategy game commentary"

    monkeypatch.setattr(conversation_jobs, "generate_conversation_title", title)

    result = conversation_jobs.generate_title_job(conversation.conversation_id)

    assert result["success"] is True
    assert result["title"] == "Ranked strategy game commentary"
    assert conversation.title == "Ranked strategy game commentary"


def test_registered_detailed_summary_job_can_skip_vault_retrieval(monkeypatch):
    conversation = _Conversation()
    detailed_contexts = []
    _patch_job_runtime(monkeypatch, conversation)

    async def detailed(*args, memory_context=None, **kwargs):
        detailed_contexts.append(memory_context)
        return "A detailed summary."

    def unexpected_memory_service():
        raise AssertionError("bulk promotion must not query the mid-rebuild vault")

    monkeypatch.setattr(conversation_jobs, "generate_detailed_summary", detailed)
    monkeypatch.setattr(
        conversation_jobs, "get_memory_service", unexpected_memory_service
    )
    result = conversation_jobs.generate_detailed_summary_job(
        conversation.conversation_id,
        include_memory_context=False,
    )

    assert result["success"] is True
    assert result["detailed_summary"] == "A detailed summary."
    assert detailed_contexts == [None]


def test_registered_detailed_summary_job_searches_only_its_memory_space(monkeypatch):
    conversation = _Conversation()
    conversation.memory_space_id = "5a265801-b8ca-4667-ae7d-07b2c170ecad"
    searched_scopes = []
    detailed_contexts = []
    _patch_job_runtime(monkeypatch, conversation)

    class _MemoryService:
        async def search_memories(
            self,
            _query,
            _user_id,
            limit=10,
            *,
            memory_space_id=None,
        ):
            searched_scopes.append(memory_space_id)
            return []

    async def detailed(*args, memory_context=None, **kwargs):
        detailed_contexts.append(memory_context)
        return "A scoped detailed summary."

    monkeypatch.setattr(conversation_jobs, "get_memory_service", _MemoryService)
    monkeypatch.setattr(conversation_jobs, "generate_detailed_summary", detailed)

    result = conversation_jobs.generate_detailed_summary_job(
        conversation.conversation_id
    )

    assert result["success"] is True
    assert searched_scopes == [conversation.memory_space_id]
    assert detailed_contexts == [None]


@pytest.mark.parametrize(
    ("job_name", "generator_name", "job_kwargs"),
    [
        ("generate_title_job", "generate_conversation_title", {}),
        ("generate_short_summary_job", "generate_short_summary", {}),
        (
            "generate_detailed_summary_job",
            "generate_detailed_summary",
            {"include_memory_context": False},
        ),
    ],
)
def test_registered_summary_jobs_finish_without_retry_on_provider_quota(
    monkeypatch, job_name, generator_name, job_kwargs
):
    conversation = _Conversation()
    _patch_job_runtime(monkeypatch, conversation)

    async def quota_exhausted(*args, **kwargs):
        request = httpx.Request("POST", "https://provider.invalid/chat/completions")
        response = httpx.Response(403, request=request)
        raise openai.PermissionDeniedError(
            "Key limit exceeded (monthly limit)",
            response=response,
            body={"error": {"code": 403}},
        )

    monkeypatch.setattr(conversation_jobs, generator_name, quota_exhausted)

    result = getattr(conversation_jobs, job_name)(
        conversation.conversation_id, **job_kwargs
    )

    assert result == {
        "success": False,
        "conversation_id": conversation.conversation_id,
        "stage": job_name.removeprefix("generate_").removesuffix("_job"),
        "reason": "provider_permission_denied",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_empty_llm_content_is_an_error_not_a_transcript_fragment(monkeypatch):
    async def prompt(*args, **kwargs):
        return "Generate a title"

    async def empty_content(*args, **kwargs):
        raise RuntimeError("LLM returned empty content; finish_reason=length")

    monkeypatch.setattr(conversation_utils, "get_user_prompt", prompt)
    monkeypatch.setattr(conversation_utils, "async_generate", empty_content)

    with pytest.raises(RuntimeError, match="empty content"):
        await conversation_utils.generate_conversation_title(
            "A transcript that must never become a fallback title."
        )


@pytest.mark.asyncio
async def test_short_transcript_uses_a_machine_detectable_title_placeholder():
    title = await conversation_utils.generate_conversation_title("Too short")

    assert title == TITLE_NOT_GENERATED
    assert title != "Too short"


def test_legacy_transcript_prefix_fallback_is_detectable():
    transcript = (
        "Oh my God! Finally. You relax. To put them at the biggest disadvantage, "
        "you can. Go get the kayak now. Everyone is reacting to the strategy and "
        "the team-switching rule."
    )

    assert conversation_utils.has_legacy_title_summary_fallback(
        title="Oh my God! Finally. You relax.",
        summary=f"{transcript[:120]}...",
        transcript=transcript,
    )
    assert not conversation_utils.has_legacy_title_summary_fallback(
        title="Survival Competition Strategy Discussion",
        summary="The group discusses a survival show.",
        transcript=transcript,
    )
