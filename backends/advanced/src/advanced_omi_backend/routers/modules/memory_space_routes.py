"""User-facing lifecycle for isolated brainstorm memory spaces."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.chat_service import get_chat_service
from advanced_omi_backend.controllers.conversation_controller import get_conversations
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import DeviceInputJob
from advanced_omi_backend.models.memory_space import (
    DeferredSpaceEvent,
    SpaceMergeProposal,
)
from advanced_omi_backend.models.vault_sync import PairRequest
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.audit import record_vault_change
from advanced_omi_backend.services.memory.scope import MemoryScope, MemoryScopeError
from advanced_omi_backend.services.memory.vault_scaffold import confined_vault_path
from advanced_omi_backend.services.memory_space_context import (
    available_screen_sources,
    frame_key,
    request_contact_sheet,
)
from advanced_omi_backend.services.memory_spaces import (
    MemorySpaceConflict,
    memory_space_service,
)
from advanced_omi_backend.services.vault_sync import (
    VaultSyncUnavailable,
    vault_sync_broker,
)
from advanced_omi_backend.users import User
from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing

router = APIRouter(prefix="/spaces", tags=["memory-spaces"])


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    seed_note_paths: list[str] = Field(default_factory=list)


class UpdateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SeedPreviewRequest(BaseModel):
    note_paths: list[str] = Field(default_factory=list)


class ResolveMergeRequest(BaseModel):
    accepted_change_ids: list[str] = Field(default_factory=list)


class PrepareMergeRequest(BaseModel):
    acknowledge_sync_warnings: bool = False


class NoteWriteRequest(BaseModel):
    content: Optional[str] = None


class SpaceChatSessionRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


class SpaceChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=100_000)


class ContextRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)


class ExtractSpaceNoteRequest(BaseModel):
    selected_frame_keys: list[str] = Field(default_factory=list, max_length=6)


def _user_id(user: User) -> str:
    return str(user.user_id)


def _space_payload(space) -> dict:
    return {
        "space_id": space.space_id,
        "name": space.name,
        "state": space.state,
        "seed_notes": [note.model_dump() for note in space.seed_notes],
        "sync_state": space.sync_state,
        "sync_error": space.sync_error,
        "merge_checkpoint": space.merge_checkpoint,
        "created_at": space.created_at,
        "updated_at": space.updated_at,
        "archived_at": space.archived_at,
    }


def _proposal_payload(proposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "space_id": proposal.space_id,
        "state": proposal.state,
        "changes": [change.model_dump() for change in proposal.changes],
        "accepted_change_ids": proposal.accepted_change_ids,
        "rejected_change_ids": proposal.rejected_change_ids,
        "deferred_event_count": proposal.deferred_event_count,
        "error": proposal.error,
        "created_at": proposal.created_at,
        "generated_at": proposal.generated_at,
        "resolved_at": proposal.resolved_at,
    }


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, VaultSyncUnavailable):
        return HTTPException(status_code=503, detail=str(error))
    if "not found" in str(error).lower():
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, MemorySpaceConflict) or "active" in str(error).lower():
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


async def _space_recording(
    user_id: str, space_id: str, conversation_id: str, *, writable: bool = False
) -> Conversation:
    await memory_space_service.resolver.require_space(
        MemoryScope(user_id, space_id), writable=writable
    )
    conversation = await Conversation.find_one(
        Conversation.user_id == user_id,
        Conversation.memory_space_id == space_id,
        Conversation.conversation_id == conversation_id,
    )
    if conversation is None:
        raise MemoryScopeError("Space recording not found")
    return conversation


async def _context_payload(conversation: Conversation) -> dict:
    sources = await available_screen_sources(conversation)
    jobs = (
        await DeviceInputJob.find(
            DeviceInputJob.user_id == conversation.user_id,
            DeviceInputJob.purpose == "memory_space_note_review",
            {"payload.conversation_id": conversation.conversation_id},
        )
        .sort("-created_at")
        .to_list()
    )
    return {
        "conversation_id": conversation.conversation_id,
        "title": conversation.title,
        "transcript": conversation.transcript or "",
        "created_at": conversation.created_at,
        "started_at": conversation.started_at,
        "ended_at": conversation.ended_at,
        "review_state": conversation.memory_review_state,
        "review_error": conversation.memory_review_error,
        "selected_frame_keys": conversation.selected_memory_context_frame_keys,
        "context_description": conversation.memory_context_description,
        "sources": [
            {
                "source_id": source.source_id,
                "name": source.name,
                "platform": source.platform,
                "status": source.status,
                "last_seen_at": source.last_seen_at,
                "health": source.health,
            }
            for source in sources
        ],
        "jobs": [
            {
                "job_id": str(job.id),
                "source_id": job.source_id,
                "status": job.status,
                "error": job.error,
                "created_at": job.created_at,
                "completed_at": job.completed_at,
            }
            for job in jobs
        ],
        "frames": [
            {
                "key": frame_key(frame.source_id, frame.frame_id),
                "source_id": frame.source_id,
                "frame_id": frame.frame_id,
                "captured_at": frame.captured_at,
                "content_type": frame.content_type,
            }
            for frame in conversation.memory_context_frames
        ],
    }


@router.get("")
async def list_spaces(user: User = Depends(current_active_user)):
    return [
        _space_payload(space)
        for space in await memory_space_service.list(_user_id(user))
    ]


@router.post("")
async def create_space(
    request: CreateSpaceRequest, user: User = Depends(current_active_user)
):
    try:
        return _space_payload(
            await memory_space_service.create(
                _user_id(user), request.name, request.seed_note_paths
            )
        )
    except (MemoryScopeError, OSError) as error:
        raise _http_error(error) from error


@router.post("/seed-preview")
async def preview_seed(
    request: SeedPreviewRequest, user: User = Depends(current_active_user)
):
    try:
        return await memory_space_service.preview_seed(
            _user_id(user), request.note_paths
        )
    except (MemoryScopeError, OSError) as error:
        raise _http_error(error) from error


@router.get("/main-notes")
async def search_main_notes(
    query: str = "",
    limit: int = 100,
    user: User = Depends(current_active_user),
):
    return await memory_space_service.search_main_notes(_user_id(user), query, limit)


@router.get("/{space_id}")
async def get_space(space_id: str, user: User = Depends(current_active_user)):
    try:
        return _space_payload(await memory_space_service.get(_user_id(user), space_id))
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.patch("/{space_id}")
async def update_space(
    space_id: str,
    request: UpdateSpaceRequest,
    user: User = Depends(current_active_user),
):
    try:
        return _space_payload(
            await memory_space_service.rename(_user_id(user), space_id, request.name)
        )
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.post("/{space_id}/reopen")
async def reopen_space(space_id: str, user: User = Depends(current_active_user)):
    try:
        return _space_payload(
            await memory_space_service.reopen(_user_id(user), space_id)
        )
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.get("/{space_id}/notes")
async def list_notes(space_id: str, user: User = Depends(current_active_user)):
    try:
        return await memory_space_service.notes(_user_id(user), space_id)
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.put("/{space_id}/notes/{note_path:path}")
async def write_note(
    space_id: str,
    note_path: str,
    request: NoteWriteRequest,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    scope = MemoryScope(user_id, space_id)
    try:
        await memory_space_service.resolver.require_space(scope, writable=True)
        root = memory_space_service.resolver.vault_root(scope)
        target = confined_vault_path(root, note_path)
        before = target.read_text(encoding="utf-8") if target.is_file() else None
        if request.content is None:
            target.unlink(missing_ok=True)
            operation = "delete"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(request.content, encoding="utf-8")
            operation = "create" if before is None else "update"
        await record_vault_change(
            user_id=user_id,
            memory_space_id=space_id,
            operation=operation,
            note_path=note_path,
            before=before,
            after=request.content,
            summary="edited in memory space",
            source_type="manual",
        )
        return {"status": operation, "note_path": note_path}
    except (MemoryScopeError, OSError, ValueError) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/search")
async def search_space(
    space_id: str,
    query: str,
    limit: int = 20,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    try:
        await memory_space_service.resolver.require_space(
            MemoryScope(user_id, space_id)
        )
        results = await get_memory_service().search_memories(
            query, user_id, limit=limit, memory_space_id=space_id
        )
        return [
            entry.model_dump() if hasattr(entry, "model_dump") else entry.__dict__
            for entry in results
        ]
    except (MemoryScopeError, ValueError) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/recordings")
async def list_recordings(
    space_id: str,
    limit: int = 200,
    offset: int = 0,
    user: User = Depends(current_active_user),
):
    try:
        await memory_space_service.resolver.require_space(
            MemoryScope(_user_id(user), space_id)
        )
        return await get_conversations(
            user,
            limit=min(max(limit, 1), 500),
            offset=max(offset, 0),
            memory_space_id=space_id,
        )
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.get("/{space_id}/recordings/{conversation_id}/note-review")
async def get_note_review(
    space_id: str,
    conversation_id: str,
    user: User = Depends(current_active_user),
):
    try:
        conversation = await _space_recording(_user_id(user), space_id, conversation_id)
        return await _context_payload(conversation)
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.post("/{space_id}/recordings/{conversation_id}/note-review/context")
async def request_note_review_context(
    space_id: str,
    conversation_id: str,
    request: ContextRequest,
    user: User = Depends(current_active_user),
):
    try:
        conversation = await _space_recording(
            _user_id(user), space_id, conversation_id, writable=True
        )
        await request_contact_sheet(conversation, request.source_id)
        return await _context_payload(conversation)
    except (MemoryScopeError, ValueError) as error:
        raise _http_error(error) from error


@router.get(
    "/{space_id}/recordings/{conversation_id}/note-review/frames/{source_id}/{frame_id}"
)
async def get_note_review_frame(
    space_id: str,
    conversation_id: str,
    source_id: str,
    frame_id: int,
    user: User = Depends(current_active_user),
):
    try:
        conversation = await _space_recording(_user_id(user), space_id, conversation_id)
    except MemoryScopeError as error:
        raise _http_error(error) from error
    frame = next(
        (
            candidate
            for candidate in conversation.memory_context_frames
            if candidate.source_id == source_id and candidate.frame_id == frame_id
        ),
        None,
    )
    if frame is None:
        raise HTTPException(status_code=404, detail="Review frame not found")
    return Response(
        content=frame.data,
        media_type=frame.content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/{space_id}/recordings/{conversation_id}/note-review/extract")
async def extract_space_note(
    space_id: str,
    conversation_id: str,
    request: ExtractSpaceNoteRequest,
    user: User = Depends(current_active_user),
):
    try:
        conversation = await _space_recording(
            _user_id(user), space_id, conversation_id, writable=True
        )
        if not conversation.has_meaningful_transcript:
            raise MemorySpaceConflict("Transcript is not ready for extraction")
        if conversation.memory_review_state in {"extracting", "extracted"}:
            raise MemorySpaceConflict(
                f"Note extraction is already {conversation.memory_review_state}"
            )
        offered = {
            frame_key(frame.source_id, frame.frame_id)
            for frame in conversation.memory_context_frames
        }
        selected = list(dict.fromkeys(request.selected_frame_keys))
        unknown = set(selected) - offered
        if unknown:
            raise MemorySpaceConflict("Selected screen context is stale")
        conversation.selected_memory_context_frame_keys = selected
        conversation.memory_review_state = "extracting"
        conversation.memory_review_error = None
        await conversation.save()
        job = enqueue_memory_processing(
            conversation.conversation_id,
            job_id=f"space_memory_{conversation.conversation_id[:12]}",
        )
        return {**(await _context_payload(conversation)), "memory_job_id": job.id}
    except (MemoryScopeError, MemorySpaceConflict, ValueError) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/chat/sessions")
async def create_chat_session(
    space_id: str,
    request: SpaceChatSessionRequest,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    try:
        await memory_space_service.resolver.require_space(
            MemoryScope(user_id, space_id), writable=True
        )
        session = await get_chat_service().create_session(
            user_id,
            request.title,
            memory_space_id=space_id,
        )
        return session.to_dict()
    except (MemoryScopeError, ValueError) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/chat/sessions")
async def list_chat_sessions(
    space_id: str,
    limit: int = 50,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    try:
        await memory_space_service.resolver.require_space(
            MemoryScope(user_id, space_id)
        )
        sessions = await get_chat_service().get_user_sessions(
            user_id,
            min(max(limit, 1), 100),
            memory_space_id=space_id,
        )
        return [session.to_dict() for session in sessions]
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.post("/{space_id}/chat/completions")
async def complete_chat(
    space_id: str,
    request: SpaceChatRequest,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    try:
        await memory_space_service.resolver.require_space(
            MemoryScope(user_id, space_id), writable=True
        )
        chat = get_chat_service()
        if request.session_id:
            session = await chat.get_session(
                request.session_id,
                user_id,
                memory_space_id=space_id,
            )
            if session is None:
                raise MemoryScopeError("Chat session not found")
        else:
            session = await chat.create_session(
                user_id,
                memory_space_id=space_id,
            )
        content = ""
        result: dict = {}
        async for event in chat.generate_response_stream(
            session.session_id,
            user_id,
            request.message,
        ):
            if event.get("type") == "token":
                content = str(event.get("data") or "")
            elif event.get("type") == "complete":
                result = event.get("data") or {}
            elif event.get("type") == "error":
                raise RuntimeError(
                    (event.get("data") or {}).get("error", "Chat failed")
                )
        return {
            "session_id": session.session_id,
            "content": content,
            **result,
        }
    except (MemoryScopeError, ValueError) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/sync")
async def sync_info(space_id: str, user: User = Depends(current_active_user)):
    user_id = _user_id(user)
    try:
        space = await memory_space_service.get(user_id, space_id)
        info = await vault_sync_broker.info(
            MemoryScope(user_id, space_id), space_name=space.name
        )
        health = await vault_sync_broker.health(
            MemoryScope(user_id, space_id), space_name=space.name
        )
        return {**info, **health}
    except (MemoryScopeError, VaultSyncUnavailable) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/sync/pair")
async def pair_sync(
    space_id: str,
    request: PairRequest,
    user: User = Depends(current_active_user),
):
    user_id = _user_id(user)
    scope = MemoryScope(user_id, space_id)
    try:
        space = await memory_space_service.resolver.require_space(scope, writable=True)
        result = await vault_sync_broker.pair(
            scope,
            device_id=request.device_id,
            device_name=request.device_name,
            space_name=space.name,
        )
        space.sync_state = "syncing"
        space.sync_error = None
        await space.save()
        return result
    except (MemoryScopeError, VaultSyncUnavailable) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/sync/rescan")
async def rescan_sync(space_id: str, user: User = Depends(current_active_user)):
    user_id = _user_id(user)
    scope = MemoryScope(user_id, space_id)
    try:
        space = await memory_space_service.get(user_id, space_id)
        await vault_sync_broker.rescan(scope, space_name=space.name)
        health = await vault_sync_broker.health(scope, space_name=space.name)
        space.sync_state = "healthy" if health["healthy"] else "error"
        space.sync_error = health.get("error")
        await space.save()
        return health
    except (MemoryScopeError, VaultSyncUnavailable) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/sync/freeze")
async def freeze_sync(space_id: str, user: User = Depends(current_active_user)):
    user_id = _user_id(user)
    scope = MemoryScope(user_id, space_id)
    try:
        space = await memory_space_service.get(user_id, space_id)
        health = await vault_sync_broker.set_frozen(scope, True, space_name=space.name)
        space.sync_state = "frozen"
        space.sync_error = None
        await space.save()
        return health
    except (MemoryScopeError, VaultSyncUnavailable) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/sync/resume")
async def resume_sync(space_id: str, user: User = Depends(current_active_user)):
    user_id = _user_id(user)
    scope = MemoryScope(user_id, space_id)
    try:
        space = await memory_space_service.resolver.require_space(scope, writable=True)
        health = await vault_sync_broker.set_frozen(scope, False, space_name=space.name)
        space.sync_state = "healthy" if health.get("healthy") else "syncing"
        space.sync_error = health.get("error")
        await space.save()
        return health
    except (MemoryScopeError, VaultSyncUnavailable) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/deferred-events")
async def list_deferred_events(
    space_id: str, user: User = Depends(current_active_user)
):
    user_id = _user_id(user)
    try:
        await memory_space_service.get(user_id, space_id)
        events = (
            await DeferredSpaceEvent.find(
                DeferredSpaceEvent.user_id == user_id,
                DeferredSpaceEvent.space_id == space_id,
            )
            .sort([("causal_order", 1), ("created_at", 1)])
            .to_list()
        )
        return [event.model_dump(mode="json") for event in events]
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.post("/{space_id}/deferred-events/{event_id}/retry")
async def retry_deferred_event(
    space_id: str,
    event_id: str,
    user: User = Depends(current_active_user),
):
    try:
        event = await memory_space_service.retry_deferred_event(
            _user_id(user), space_id, event_id
        )
        return event.model_dump(mode="json")
    except (MemoryScopeError, MemorySpaceConflict) as error:
        raise _http_error(error) from error


@router.post("/{space_id}/merge-proposals")
async def prepare_merge(
    space_id: str,
    request: PrepareMergeRequest,
    user: User = Depends(current_active_user),
):
    try:
        return _proposal_payload(
            await memory_space_service.prepare_merge(
                _user_id(user),
                space_id,
                acknowledge_sync_warnings=request.acknowledge_sync_warnings,
            )
        )
    except (MemoryScopeError, MemorySpaceConflict) as error:
        raise _http_error(error) from error


@router.get("/{space_id}/merge-proposals/latest")
async def latest_merge_proposal(
    space_id: str, user: User = Depends(current_active_user)
):
    user_id = _user_id(user)
    try:
        await memory_space_service.get(user_id, space_id)
        proposals = (
            await SpaceMergeProposal.find(
                SpaceMergeProposal.user_id == user_id,
                SpaceMergeProposal.space_id == space_id,
            )
            .sort([("created_at", -1)])
            .limit(1)
            .to_list()
        )
        return _proposal_payload(proposals[0]) if proposals else None
    except MemoryScopeError as error:
        raise _http_error(error) from error


@router.post("/merge-proposals/{proposal_id}/resolve")
async def resolve_merge(
    proposal_id: str,
    request: ResolveMergeRequest,
    user: User = Depends(current_active_user),
):
    try:
        return _proposal_payload(
            await memory_space_service.resolve_merge(
                _user_id(user), proposal_id, request.accepted_change_ids
            )
        )
    except (MemoryScopeError, MemorySpaceConflict) as error:
        raise _http_error(error) from error
