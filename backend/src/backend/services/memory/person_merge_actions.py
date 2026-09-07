"""Authenticated-service orchestration for deterministic person merges."""

import asyncio

from .audit import MemoryCause, memory_provenance, record_vault_change
from .person_identity import IdentityChangeResult, PersonIdentityService
from .person_merge import PersonMergeResult, PersonMergeService
from .vault_manager import ConvDocVaultManager


def _service(user_id: str) -> PersonMergeService:
    return PersonMergeService(ConvDocVaultManager().user_root(user_id))


def _identity_service(user_id: str) -> PersonIdentityService:
    return PersonIdentityService(ConvDocVaultManager().user_root(user_id))


async def get_person_suggestions(user_id: str, limit: int = 20) -> list[dict]:
    return await asyncio.to_thread(_identity_service(user_id).suggestions, limit)


async def set_people_distinct(
    user_id: str,
    person_a: str,
    person_b: str,
    *,
    distinct: bool,
    revision: str | None = None,
) -> dict:
    result = await asyncio.to_thread(
        _identity_service(user_id).set_distinct,
        person_a,
        person_b,
        distinct=distinct,
        revision=revision,
    )
    await _record_identity_audit(user_id, result)
    return result.to_dict()


async def preview_person_merge(
    user_id: str,
    source_name: str,
    target_name: str,
    source_hash: str | None = None,
    target_hash: str | None = None,
) -> dict:
    preview = await asyncio.to_thread(
        _service(user_id).preview,
        source_name,
        target_name,
        expected_source_hash=source_hash,
        expected_target_hash=target_hash,
    )
    return preview.to_dict()


async def apply_person_merge(
    user_id: str, source_name: str, target_name: str, plan_token: str
) -> dict:
    result = await asyncio.to_thread(
        _service(user_id).apply, source_name, target_name, plan_token
    )
    await _record_merge_audit(user_id, result)
    return result.to_dict()


async def _record_merge_audit(user_id: str, result: PersonMergeResult) -> None:
    action_id = result.action_id
    source_path = result.preview.source_path
    target_path = result.preview.target_path
    with memory_provenance(MemoryCause.OBSIDIAN_ACTION):
        for path in result.changed_paths:
            before = result.before.get(path)
            after = result.after.get(path)
            if path == source_path:
                await record_vault_change(
                    user_id=user_id,
                    operation="rename",
                    note_path=path,
                    before=before,
                    after=None,
                    summary=f"merged into {target_path}",
                    action_id=action_id,
                    new_path=target_path,
                )
                continue
            await record_vault_change(
                user_id=user_id,
                operation="update",
                note_path=path,
                before=before,
                after=after,
                summary=(
                    f"person merge {result.preview.source_name} → "
                    f"{result.preview.target_name}"
                ),
                action_id=action_id,
                source_path=source_path,
                target_path=target_path,
            )


async def _record_identity_audit(user_id: str, result: IdentityChangeResult) -> None:
    with memory_provenance(MemoryCause.OBSIDIAN_ACTION):
        for path in result.changed_paths:
            await record_vault_change(
                user_id=user_id,
                operation="update",
                note_path=path,
                before=result.before[path],
                after=result.after[path],
                summary=(
                    f"identity decision: {result.person_a} and {result.person_b} are "
                    f"{'separate people' if result.decision == 'distinct' else 'no longer marked separate'}"
                ),
                action_id=result.action_id,
                identity_decision=result.decision,
                person_a=result.person_a,
                person_b=result.person_b,
            )
