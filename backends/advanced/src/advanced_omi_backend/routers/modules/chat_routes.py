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
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.chat_service import ChatSession, get_chat_service
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# Pydantic models for API
class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description="The user's message")
    session_id: Optional[str] = Field(None, description="Session ID (creates new session if not provided)")
    include_obsidian_memory: Optional[bool] = Field(False, description="Whether to include context from Obsidian vault")


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
    title: str = Field(..., min_length=1, max_length=200, description="New session title")


class ChatStatisticsResponse(BaseModel):
    total_sessions: int
    total_messages: int
    last_chat: Optional[str] = None


@router.post("/sessions", response_model=ChatSessionResponse)
async def create_chat_session(
    request: ChatSessionCreateRequest,
    current_user: User = Depends(current_active_user)
):
    """Create a new chat session."""
    try:
        chat_service = get_chat_service()
        session = await chat_service.create_session(
            user_id=str(current_user.id),
            title=request.title
        )
        
        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat()
        )
    except Exception as e:
        logger.error(f"Failed to create chat session for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create chat session"
        )


@router.get("/sessions", response_model=List[ChatSessionResponse])
async def get_chat_sessions(
    limit: int = 50,
    current_user: User = Depends(current_active_user)
):
    """Get all chat sessions for the current user."""
    try:
        chat_service = get_chat_service()
        sessions = await chat_service.get_user_sessions(
            user_id=str(current_user.id),
            limit=min(limit, 100)  # Cap at 100
        )
        
        # Get message counts for each session (this could be optimized with aggregation)
        session_responses = []
        for session in sessions:
            messages = await chat_service.get_session_messages(
                session_id=session.session_id,
                user_id=str(current_user.id),
                limit=1  # We just need count, but MongoDB doesn't have efficient count
            )
            
            session_responses.append(ChatSessionResponse(
                session_id=session.session_id,
                title=session.title,
                created_at=session.created_at.isoformat(),
                updated_at=session.updated_at.isoformat(),
                message_count=len(messages)  # This is approximate for efficiency
            ))
        
        return session_responses
    except Exception as e:
        logger.error(f"Failed to get chat sessions for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat sessions"
        )


@router.get("/sessions/{session_id}", response_model=ChatSessionResponse)
async def get_chat_session(
    session_id: str,
    current_user: User = Depends(current_active_user)
):
    """Get a specific chat session."""
    try:
        chat_service = get_chat_service()
        session = await chat_service.get_session(session_id, str(current_user.id))
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat session not found"
            )
        
        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get chat session {session_id} for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat session"
        )


@router.put("/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_chat_session(
    session_id: str,
    request: ChatSessionUpdateRequest,
    current_user: User = Depends(current_active_user)
):
    """Update a chat session's title."""
    try:
        chat_service = get_chat_service()
        
        # Verify session exists and belongs to user
        session = await chat_service.get_session(session_id, str(current_user.id))
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat session not found"
            )
        
        # Update the title
        success = await chat_service.update_session_title(
            session_id, str(current_user.id), request.title
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update session title"
            )
        
        # Return updated session
        updated_session = await chat_service.get_session(session_id, str(current_user.id))
        return ChatSessionResponse(
            session_id=updated_session.session_id,
            title=updated_session.title,
            created_at=updated_session.created_at.isoformat(),
            updated_at=updated_session.updated_at.isoformat()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update chat session {session_id} for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update chat session"
        )


@router.delete("/sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    current_user: User = Depends(current_active_user)
):
    """Delete a chat session and all its messages."""
    try:
        chat_service = get_chat_service()
        success = await chat_service.delete_session(session_id, str(current_user.id))
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat session not found"
            )
        
        return {"message": "Chat session deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete chat session {session_id} for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete chat session"
        )


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
async def get_session_messages(
    session_id: str,
    limit: int = 100,
    current_user: User = Depends(current_active_user)
):
    """Get all messages in a chat session."""
    try:
        chat_service = get_chat_service()
        
        # Verify session exists and belongs to user
        session = await chat_service.get_session(session_id, str(current_user.id))
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat session not found"
            )
        
        messages = await chat_service.get_session_messages(
            session_id=session_id,
            user_id=str(current_user.id),
            limit=min(limit, 200)  # Cap at 200
        )
        
        return [
            ChatMessageResponse(
                message_id=msg.message_id,
                session_id=msg.session_id,
                role=msg.role,
                content=msg.content,
                timestamp=msg.timestamp.isoformat(),
                memories_used=msg.memories_used
            )
            for msg in messages
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get messages for session {session_id}, user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve messages"
        )


@router.post("/send")
async def send_message_stream(
    request: ChatMessageRequest,
    current_user: User = Depends(current_active_user)
):
    """Send a message and receive streaming response via Server-Sent Events."""
    try:
        chat_service = get_chat_service()
        
        # Create new session if not provided
        if not request.session_id:
            session = await chat_service.create_session(str(current_user.id))
            session_id = session.session_id
        else:
            session_id = request.session_id
            # Verify session belongs to user
            session = await chat_service.get_session(session_id, str(current_user.id))
            if not session:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Chat session not found"
                )
        
        # Create SSE streaming response
        async def event_stream():
            try:
                async for event in chat_service.generate_response_stream(
                    session_id=session_id,
                    user_id=str(current_user.id),
                    message_content=request.message,
                    include_obsidian_memory=request.include_obsidian_memory
                ):
                    # Format as Server-Sent Event
                    event_data = json.dumps(event, default=str)
                    yield f"data: {event_data}\n\n"
                
                # Send final event to close connection
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                logger.error(f"Error in streaming response: {e}")
                error_event = {
                    "type": "error",
                    "data": {"error": str(e)},
                    "timestamp": time.time()
                }
                yield f"data: {json.dumps(error_event)}\n\n"
        
        return StreamingResponse(
            event_stream(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process message for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message"
        )


@router.get("/statistics", response_model=ChatStatisticsResponse)
async def get_chat_statistics(
    current_user: User = Depends(current_active_user)
):
    """Get chat statistics for the current user."""
    try:
        chat_service = get_chat_service()
        stats = await chat_service.get_chat_statistics(str(current_user.id))
        
        return ChatStatisticsResponse(
            total_sessions=stats["total_sessions"],
            total_messages=stats["total_messages"],
            last_chat=stats["last_chat"].isoformat() if stats["last_chat"] else None
        )
    except Exception as e:
        logger.error(f"Failed to get chat statistics for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat statistics"
        )


@router.post("/sessions/{session_id}/extract-memories")
async def extract_memories_from_session(
    session_id: str,
    current_user: User = Depends(current_active_user)
):
    """Extract memories from a chat session."""
    try:
        chat_service = get_chat_service()
        
        # Extract memories from the session
        success, memory_ids, memory_count = await chat_service.extract_memories_from_session(
            session_id=session_id,
            user_id=str(current_user.id)
        )
        
        if success:
            return {
                "success": True,
                "memory_ids": memory_ids,
                "count": memory_count,
                "message": f"Successfully extracted {memory_count} memories from chat session"
            }
        else:
            return {
                "success": False,
                "memory_ids": [],
                "count": 0,
                "message": "Failed to extract memories from chat session"
            }
        
    except Exception as e:
        logger.error(f"Failed to extract memories from session {session_id} for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract memories from chat session"
        )


@router.get("/health")
async def chat_health_check():
    """Health check endpoint for chat service."""
    try:
        chat_service = get_chat_service()
        # Simple health check - verify service can be initialized
        if not chat_service._initialized:
            await chat_service.initialize()
        
        return {
            "status": "healthy",
            "service": "chat",
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Chat service health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat service is not available"
        )