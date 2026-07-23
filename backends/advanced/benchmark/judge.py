"""LLM-as-judge for LongMemEval — verbatim from the paper's evaluate_qa.py.

Source: https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py

The paper invokes ``gpt-4o-2024-08-06`` with ``temperature=0`` and parses
``"yes" in response.lower()``. We keep the exact prompt templates and parse
behaviour. The call routes through Chronicle's ``llm_client``, so the
configured registry's default LLM is used unless a caller resolves a
specific judge model (e.g., by registering an ``llm_judge`` operation) and
passes ``model=``.

Cache: keyed on (question, answer, ground_truth, model, question_type,
abstention) — re-running scoring against the same model is free.
"""

from __future__ import annotations

import logging
from typing import Optional

from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.model_registry import get_models_registry

from .progress import JudgeCache

logger = logging.getLogger(__name__)


_STD_TEMPLATE_DEFAULT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also "
    "answer yes. If the response only contains a subset of the information "
    "required by the answer, answer no. \n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_STD_TEMPLATE_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also "
    "answer yes. If the response only contains a subset of the information "
    "required by the answer, answer no. In addition, do not penalize off-by-one "
    "errors for the number of days. If the question asks for the number of "
    "days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
    "predicting 19 days when the answer is 18), the model's response is still "
    "correct. \n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_STD_TEMPLATE_KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response contains some previous information along with "
    "an updated answer, the response should be considered as correct as long as "
    "the updated answer is the required answer.\n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_STD_TEMPLATE_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, "
    "and a response from a model. Please answer yes if the response satisfies "
    "the desired response. Otherwise, answer no. The model does not need to "
    "reflect all the points in the rubric. The response is correct as long as "
    "it recalls and utilizes the user's personal information correctly.\n\n"
    "Question: {}\n\nRubric: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_ABSTENTION_TEMPLATE = (
    "I will give you an unanswerable question, an explanation, and a response "
    "from a model. Please answer yes if the model correctly identifies the "
    "question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information "
    "is not.\n\n"
    "Question: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer "
    "yes or no only."
)


def get_anscheck_prompt(
    *, question_type: str, question: str, answer: str, response: str, abstention: bool
) -> str:
    """Build the judge prompt for one (question, answer, response) triple."""
    if abstention:
        return _ABSTENTION_TEMPLATE.format(question, answer, response)
    if question_type in ("single-session-user", "single-session-assistant", "multi-session"):
        return _STD_TEMPLATE_DEFAULT.format(question, answer, response)
    if question_type == "temporal-reasoning":
        return _STD_TEMPLATE_TEMPORAL.format(question, answer, response)
    if question_type == "knowledge-update":
        return _STD_TEMPLATE_KNOWLEDGE_UPDATE.format(question, answer, response)
    if question_type == "single-session-preference":
        return _STD_TEMPLATE_PREFERENCE.format(question, answer, response)
    raise NotImplementedError(f"Unknown question_type {question_type!r}")


def resolve_judge_model() -> str:
    """Resolve the judge model name via Chronicle's ``model_registry``.

    Prefers a registered ``llm_judge`` operation/role; falls back to the
    default ``llm`` model. The paper uses ``gpt-4o-2024-08-06`` — register
    that as ``llm_judge`` for parity, otherwise the configured LLM is used.
    """
    registry = get_models_registry()
    if registry is not None:
        try:
            op = registry.get_llm_operation("llm_judge")
            if op and op.model_name:
                return op.model_name
        except Exception:
            pass
        default = registry.get_default("llm")
        if default and default.model_name:
            return default.model_name
    return get_llm_client().get_default_model()


def judge_answer(
    *,
    question_id: str,
    question_type: str,
    question: str,
    ground_truth: str,
    answer: str,
    abstention: bool,
    cache: JudgeCache,
    model: Optional[str] = None,
) -> tuple[bool, str, str]:
    """Score one answer; returns ``(is_correct, raw_response, judge_model)``.

    Caches successful judge replies on disk. Uses ``temperature=0`` per the
    paper. Reasoning models that don't accept ``temperature`` are handled
    by ``llm_client.generate`` (drops the param transparently).
    """
    judge_model = model or resolve_judge_model()
    prompt = get_anscheck_prompt(
        question_type=question_type,
        question=question,
        answer=ground_truth,
        response=answer,
        abstention=abstention,
    )

    key = JudgeCache.make_key(
        question=question,
        answer=answer,
        ground_truth=ground_truth,
        model=judge_model,
        question_type=question_type,
        abstention=abstention,
    )
    cached = cache.get(key)
    if cached is not None:
        return bool(cached["label"]), cached["raw"], judge_model

    client = get_llm_client()
    raw = client.generate(prompt=prompt, model=judge_model, temperature=0.0)
    label = "yes" in raw.strip().lower()
    cache.put(key, {"label": label, "raw": raw, "model": judge_model, "question_id": question_id})
    return label, raw, judge_model
