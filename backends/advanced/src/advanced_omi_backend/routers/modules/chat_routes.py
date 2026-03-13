"""
Chat API routes for Chronicle with streaming support and memory integration.

This module provides:
- RESTful chat session management endpoints
- Server-Sent Events (SSE) for streaming responses
- Memory-enhanced conversational AI
- User-scoped data isolation
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.chat_service import ChatSession, get_chat_service
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# Pydantic models for API

# --- OpenAI-compatible chat completion models ---


class ChatCompletionMessage(BaseModel):
    role: str = Field(
        ..., description="The role of the message author (system, user, assistant)"
    )
    content: str = Field(..., description="The message content")


class ChatCompletionRequest(BaseModel):
    messages: List[ChatCompletionMessage] = Field(
        ..., min_length=1, description="List of messages in the conversation"
    )
    model: Optional[str] = Field(
        None, description="Model to use (ignored, uses server-configured model)"
    )
    stream: Optional[bool] = Field(True, description="Whether to stream the response")
    temperature: Optional[float] = Field(
        None, description="Sampling temperature (ignored, uses server config)"
    )
    session_id: Optional[str] = Field(
        None, description="Chronicle session ID (creates new if not provided)"
    )
    include_obsidian_memory: Optional[bool] = Field(
        False, description="Whether to include Obsidian vault context"
    )


class ChatCompletionChunkDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]
    chronicle_metadata: Optional[Dict[str, Any]] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[Dict[str, int]] = None
    session_id: Optional[str] = None
    chronicle_metadata: Optional[Dict[str, Any]] = None


class ChatMessageResponse(BaseModel):
    message_id: str
    session_id: str
    role: str
    content: str
    timestamp: str
    memories_used: List[str] = []


class ChatSessionResponse(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: Optional[int] = 0


class ChatSessionCreateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=200, description="Session title")


class ChatSessionUpdateRequest(BaseModel):
    title: str = Field(
        ..., min_length=1, max_length=200, description="New session title"
    )


class ChatStatisticsResponse(BaseModel):
    total_sessions: int
    total_messages: int
    last_chat: Optional[str] = None


@router.post("/sessions", response_model=ChatSessionResponse)
async def create_chat_session(
    request: ChatSessionCreateRequest, current_user: User = Depends(current_active_user)
):
    """Create a new chat session."""
    try:
        chat_service = get_chat_service()
        session = await chat_service.create_session(
            user_id=str(current_user.id), title=request.title
        )

        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
        )
    except Exception as e:
        logger.error(f"Failed to create chat session for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create chat session",
        )


@router.get("/sessions", response_model=List[ChatSessionResponse])
async def get_chat_sessions(
    limit: int = 50, current_user: User = Depends(current_active_user)
):
    """Get all chat sessions for the current user."""
    try:
        chat_service = get_chat_service()
        sessions = await chat_service.get_user_sessions(
            user_id=str(current_user.id), limit=min(limit, 100)  # Cap at 100
        )

        # Get message counts for each session (this could be optimized with aggregation)
        session_responses = []
        for session in sessions:
            messages = await chat_service.get_session_messages(
                session_id=session.session_id,
                user_id=str(current_user.id),
                limit=1,  # We just need count, but MongoDB doesn't have efficient count
            )

            session_responses.append(
                ChatSessionResponse(
                    session_id=session.session_id,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    updated_at=session.updated_at.isoformat(),
                    message_count=len(messages),  # This is approximate for efficiency
                )
            )

        return session_responses
    except Exception as e:
        logger.error(f"Failed to get chat sessions for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat sessions",
        )


@router.get("/sessions/{session_id}", response_model=ChatSessionResponse)
async def get_chat_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    """Get a specific chat session."""
    try:
        chat_service = get_chat_service()
        session = await chat_service.get_session(session_id, str(current_user.id))

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get chat session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat session",
        )


@router.put("/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_chat_session(
    session_id: str,
    request: ChatSessionUpdateRequest,
    current_user: User = Depends(current_active_user),
):
    """Update a chat session's title."""
    try:
        chat_service = get_chat_service()

        # Verify session exists and belongs to user
        session = await chat_service.get_session(session_id, str(current_user.id))
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        # Update the title
        success = await chat_service.update_session_title(
            session_id, str(current_user.id), request.title
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update session title",
            )

        # Return updated session
        updated_session = await chat_service.get_session(
            session_id, str(current_user.id)
        )
        return ChatSessionResponse(
            session_id=updated_session.session_id,
            title=updated_session.title,
            created_at=updated_session.created_at.isoformat(),
            updated_at=updated_session.updated_at.isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to update chat session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update chat session",
        )


@router.delete("/sessions/{session_id}")
async def delete_chat_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    """Delete a chat session and all its messages."""
    try:
        chat_service = get_chat_service()
        success = await chat_service.delete_session(session_id, str(current_user.id))

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        return {"message": "Chat session deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to delete chat session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete chat session",
        )


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
async def get_session_messages(
    session_id: str, limit: int = 100, current_user: User = Depends(current_active_user)
):
    """Get all messages in a chat session."""
    try:
        chat_service = get_chat_service()

        # Verify session exists and belongs to user
        session = await chat_service.get_session(session_id, str(current_user.id))
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        messages = await chat_service.get_session_messages(
            session_id=session_id,
            user_id=str(current_user.id),
            limit=min(limit, 200),  # Cap at 200
        )

        return [
            ChatMessageResponse(
                message_id=msg.message_id,
                session_id=msg.session_id,
                role=msg.role,
                content=msg.content,
                timestamp=msg.timestamp.isoformat(),
                memories_used=msg.memories_used,
            )
            for msg in messages
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get messages for session {session_id}, user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve messages",
        )


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest, current_user: User = Depends(current_active_user)
):
    """OpenAI-compatible chat completions endpoint with streaming support."""
    try:
        chat_service = get_chat_service()

        # Create new session if not provided
        if not request.session_id:
            session = await chat_service.create_session(str(current_user.id))
            session_id = session.session_id
        else:
            session_id = request.session_id
            session = await chat_service.get_session(session_id, str(current_user.id))
            if not session:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Chat session not found",
                )

        # Extract the latest user message
        user_messages = [m for m in request.messages if m.role == "user"]
        if not user_messages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one user message is required",
            )
        message_content = user_messages[-1].content

        model_name = getattr(chat_service.llm_client, "model", None) or "chronicle"
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if request.stream:
            return StreamingResponse(
                _stream_openai_format(
                    chat_service,
                    session_id,
                    str(current_user.id),
                    message_content,
                    request.include_obsidian_memory,
                    completion_id,
                    created,
                    model_name,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            return await _non_streaming_response(
                chat_service,
                session_id,
                str(current_user.id),
                message_content,
                request.include_obsidian_memory,
                completion_id,
                created,
                model_name,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process message for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message",
        )


async def _stream_openai_format(
    chat_service,
    session_id: str,
    user_id: str,
    message_content: str,
    include_obsidian_memory: bool,
    completion_id: str,
    created: int,
    model_name: str,
):
    """Map internal streaming events to OpenAI SSE chunk format."""
    previous_text = ""
    try:
        async for event in chat_service.generate_response_stream(
            session_id=session_id,
            user_id=user_id,
            message_content=message_content,
            include_obsidian_memory=include_obsidian_memory,
        ):
            event_type = event.get("type")

            if event_type == "memory_context":
                # First chunk: send role + chronicle metadata
                chunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created,
                    model=model_name,
                    choices=[
                        ChatCompletionChunkChoice(
                            delta=ChatCompletionChunkDelta(role="assistant"),
                        )
                    ],
                    chronicle_metadata={
                        "session_id": session_id,
                        **event["data"],
                    },
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

            elif event_type == "token":
                # Internal events carry accumulated text; compute delta
                accumulated = event["data"]
                delta_text = accumulated[len(previous_text) :]
                previous_text = accumulated
                if delta_text:
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=model_name,
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(content=delta_text),
                            )
                        ],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"

            elif event_type == "complete":
                chunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created,
                    model=model_name,
                    choices=[
                        ChatCompletionChunkChoice(
                            delta=ChatCompletionChunkDelta(),
                            finish_reason="stop",
                        )
                    ],
                    chronicle_metadata={
                        "session_id": session_id,
                        "message_id": event["data"].get("message_id"),
                        "memories_used": event["data"].get("memories_used", []),
                    },
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

            elif event_type == "error":
                error_obj = {
                    "error": {
                        "message": event["data"].get("error", "Unknown error"),
                        "type": "server_error",
                    }
                }
                yield f"data: {json.dumps(error_obj)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Error in streaming response: {e}")
        error_obj = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_obj)}\n\n"


async def _non_streaming_response(
    chat_service,
    session_id: str,
    user_id: str,
    message_content: str,
    include_obsidian_memory: bool,
    completion_id: str,
    created: int,
    model_name: str,
) -> ChatCompletionResponse:
    """Collect all events and return a single ChatCompletionResponse."""
    full_content = ""
    metadata: Dict[str, Any] = {"session_id": session_id}

    async for event in chat_service.generate_response_stream(
        session_id=session_id,
        user_id=user_id,
        message_content=message_content,
        include_obsidian_memory=include_obsidian_memory,
    ):
        event_type = event.get("type")

        if event_type == "memory_context":
            metadata.update(event["data"])
        elif event_type == "token":
            full_content = event["data"]  # accumulated text
        elif event_type == "complete":
            metadata["message_id"] = event["data"].get("message_id")
            metadata["memories_used"] = event["data"].get("memories_used", [])
        elif event_type == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=event["data"].get("error", "Unknown error"),
            )

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_name,
        choices=[
            ChatCompletionChoice(
                message=ChatCompletionMessage(
                    role="assistant", content=full_content.strip()
                ),
            )
        ],
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        session_id=session_id,
        chronicle_metadata=metadata,
    )


@router.get("/statistics", response_model=ChatStatisticsResponse)
async def get_chat_statistics(current_user: User = Depends(current_active_user)):
    """Get chat statistics for the current user."""
    try:
        chat_service = get_chat_service()
        stats = await chat_service.get_chat_statistics(str(current_user.id))

        return ChatStatisticsResponse(
            total_sessions=stats["total_sessions"],
            total_messages=stats["total_messages"],
            last_chat=stats["last_chat"].isoformat() if stats["last_chat"] else None,
        )
    except Exception as e:
        logger.error(f"Failed to get chat statistics for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat statistics",
        )


@router.post("/sessions/{session_id}/extract-memories")
async def extract_memories_from_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    """Extract memories from a chat session."""
    try:
        chat_service = get_chat_service()

        # Extract memories from the session
        success, memory_ids, memory_count = (
            await chat_service.extract_memories_from_session(
                session_id=session_id, user_id=str(current_user.id)
            )
        )

        if success:
            return {
                "success": True,
                "memory_ids": memory_ids,
                "count": memory_count,
                "message": f"Successfully extracted {memory_count} memories from chat session",
            }
        else:
            return {
                "success": False,
                "memory_ids": [],
                "count": 0,
                "message": "Failed to extract memories from chat session",
            }

    except Exception as e:
        logger.error(
            f"Failed to extract memories from session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract memories from chat session",
        )


@router.get("/health")
async def chat_health_check():
    """Health check endpoint for chat service."""
    try:
        chat_service = get_chat_service()
        # Simple health check - verify service can be initialized
        if not chat_service._initialized:
            await chat_service.initialize()

        return {"status": "healthy", "service": "chat", "timestamp": time.time()}
    except Exception as e:
        logger.error(f"Chat service health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat service is not available",
        )
