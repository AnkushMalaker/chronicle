"""
Chat service implementation for Chronicle with memory integration.

This module provides:
- Chat session management with MongoDB persistence
- Memory-enhanced RAG for contextual responses
- Streaming LLM responses with proper error handling
- Integration with existing mem0 memory infrastructure
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection

from advanced_omi_backend.database import get_database
from advanced_omi_backend.llm_client import get_llm_client
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.base import MemoryEntry
from advanced_omi_backend.services.obsidian_service import (
    get_obsidian_service,
    ObsidianSearchError,
)
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

# Configuration
MAX_MEMORY_CONTEXT = 5  # Maximum number of memories to include in context
MAX_CONVERSATION_HISTORY = 10  # Maximum conversation turns to keep in context


class ChatMessage:
    """Represents a chat message."""
    
    def __init__(
        self,
        message_id: str,
        session_id: str,
        user_id: str,
        role: str,  # 'user' or 'assistant'
        content: str,
        timestamp: datetime = None,
        memories_used: List[str] = None,
        metadata: Dict = None,
    ):
        self.message_id = message_id
        self.session_id = session_id
        self.user_id = user_id
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.utcnow()
        self.memories_used = memories_used or []
        self.metadata = metadata or {}

    def to_dict(self) -> Dict:
        """Convert message to dictionary for storage."""
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "memories_used": self.memories_used,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ChatMessage":
        """Create message from dictionary."""
        return cls(
            message_id=data["message_id"],
            session_id=data["session_id"],
            user_id=data["user_id"],
            role=data["role"],
            content=data["content"],
            timestamp=data["timestamp"],
            memories_used=data.get("memories_used", []),
            metadata=data.get("metadata", {}),
        )


class ChatSession:
    """Represents a chat session."""
    
    def __init__(
        self,
        session_id: str,
        user_id: str,
        title: str = None,
        created_at: datetime = None,
        updated_at: datetime = None,
        metadata: Dict = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.title = title or "New Chat"
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()
        self.metadata = metadata or {}

    def to_dict(self) -> Dict:
        """Convert session to dictionary for storage."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ChatSession":
        """Create session from dictionary."""
        return cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            title=data.get("title", "New Chat"),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            metadata=data.get("metadata", {}),
        )


class ChatService:
    """Service for managing chat sessions and memory-enhanced conversations."""
    
    def __init__(self):
        self.db = None
        self.sessions_collection: Optional[AsyncIOMotorCollection] = None
        self.messages_collection: Optional[AsyncIOMotorCollection] = None
        self.llm_client = None
        self.memory_service = None
        self._initialized = False

    async def initialize(self):
        """Initialize the chat service with database connections."""
        if self._initialized:
            return

        try:
            # Get database connection
            self.db = get_database()
            self.sessions_collection = self.db["chat_sessions"]
            self.messages_collection = self.db["chat_messages"]

            # Create indexes for better performance
            await self.sessions_collection.create_index([("user_id", 1), ("updated_at", -1)])
            await self.messages_collection.create_index([("session_id", 1), ("timestamp", 1)])
            await self.messages_collection.create_index([("user_id", 1), ("timestamp", -1)])

            # Initialize LLM client and memory service
            self.llm_client = get_llm_client()
            self.memory_service = get_memory_service()

            self._initialized = True
            logger.info("Chat service initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize chat service: {e}")
            raise

    async def create_session(self, user_id: str, title: str = None) -> ChatSession:
        """Create a new chat session."""
        if not self._initialized:
            await self.initialize()

        session = ChatSession(
            session_id=str(uuid4()),
            user_id=user_id,
            title=title or "New Chat"
        )

        await self.sessions_collection.insert_one(session.to_dict())
        logger.info(f"Created new chat session {session.session_id} for user {user_id}")
        return session

    async def get_user_sessions(self, user_id: str, limit: int = 50) -> List[ChatSession]:
        """Get all chat sessions for a user."""
        if not self._initialized:
            await self.initialize()

        cursor = self.sessions_collection.find(
            {"user_id": user_id}
        ).sort("updated_at", -1).limit(limit)

        sessions = []
        async for doc in cursor:
            sessions.append(ChatSession.from_dict(doc))

        return sessions

    async def get_session(self, session_id: str, user_id: str) -> Optional[ChatSession]:
        """Get a specific chat session."""
        if not self._initialized:
            await self.initialize()

        doc = await self.sessions_collection.find_one({
            "session_id": session_id,
            "user_id": user_id
        })

        if doc:
            return ChatSession.from_dict(doc)
        return None

    async def delete_session(self, session_id: str, user_id: str) -> bool:
        """Delete a chat session and all its messages."""
        if not self._initialized:
            await self.initialize()

        # Delete all messages in the session
        await self.messages_collection.delete_many({
            "session_id": session_id,
            "user_id": user_id
        })

        # Delete the session
        result = await self.sessions_collection.delete_one({
            "session_id": session_id,
            "user_id": user_id
        })

        success = result.deleted_count > 0
        if success:
            logger.info(f"Deleted chat session {session_id} for user {user_id}")
        return success

    async def get_session_messages(
        self, session_id: str, user_id: str, limit: int = 100
    ) -> List[ChatMessage]:
        """Get all messages in a chat session."""
        if not self._initialized:
            await self.initialize()

        cursor = self.messages_collection.find({
            "session_id": session_id,
            "user_id": user_id
        }).sort("timestamp", 1).limit(limit)

        messages = []
        async for doc in cursor:
            messages.append(ChatMessage.from_dict(doc))

        return messages

    async def add_message(self, message: ChatMessage) -> bool:
        """Add a message to the chat session."""
        if not self._initialized:
            await self.initialize()

        try:
            await self.messages_collection.insert_one(message.to_dict())
            
            # Update session timestamp and title if needed
            update_data = {"updated_at": message.timestamp}
            
            # Auto-generate title from first user message if session has default title
            if message.role == "user":
                session = await self.get_session(message.session_id, message.user_id)
                if session and session.title == "New Chat":
                    # Use first 50 characters of user message as title
                    title = message.content[:50].strip()
                    if len(message.content) > 50:
                        title += "..."
                    update_data["title"] = title

            await self.sessions_collection.update_one(
                {"session_id": message.session_id, "user_id": message.user_id},
                {"$set": update_data}
            )
            
            return True
        except Exception as e:
            logger.error(f"Failed to add message to session {message.session_id}: {e}")
            return False

    async def get_relevant_memories(self, query: str, user_id: str) -> List[MemoryEntry]:
        """Get relevant memories for the user's query."""
        try:
            memories = await self.memory_service.search_memories(
                query=query, 
                user_id=user_id, 
                limit=MAX_MEMORY_CONTEXT
            )
            logger.info(f"Retrieved {len(memories)} relevant memories for query: {query[:50]}...")
            return memories
        except Exception as e:
            logger.error(f"Failed to retrieve memories for user {user_id}: {e}")
            return []

    async def format_conversation_context(
        self, session_id: str, user_id: str, current_message: str, include_obsidian_memory: bool = False
    ) -> Tuple[str, List[str]]:
        """Format conversation context with memory integration."""
        # Get recent conversation history
        messages = await self.get_session_messages(session_id, user_id, MAX_CONVERSATION_HISTORY)
        
        # Get relevant memories
        memories = await self.get_relevant_memories(current_message, user_id)
        memory_ids = [memory.id for memory in memories if memory.id]

        # Build context string
        context_parts = []

        # Add memory context if available
        if memories:
            context_parts.append("# Relevant Personal Memories:")
            for i, memory in enumerate(memories, 1):
                memory_text = memory.content
                if memory_text:
                    context_parts.append(f"{i}. {memory_text}")
            context_parts.append("")

        # Add Obsidian context if requested
        if include_obsidian_memory:
            try:
                obsidian_service = get_obsidian_service()
                obsidian_result = await obsidian_service.search_obsidian(current_message)
                obsidian_context = obsidian_result["results"]
                if obsidian_context:
                    context_parts.append("# Relevant Obsidian Notes:")
                    for entry in obsidian_context:
                        context_parts.append(entry)
                    context_parts.append("")
                    logger.info(f"Added {len(obsidian_context)} Obsidian notes to context")
            except ObsidianSearchError as exc:
                logger.error(
                    "Failed to get Obsidian context (%s stage): %s",
                    exc.stage,
                    exc,
                )
                raise
            except Exception as e:
                logger.error(f"Failed to get Obsidian context: {e}")
                raise e

        # Add conversation history
        if messages:
            context_parts.append("# Recent Conversation:")
            for msg in messages[-MAX_CONVERSATION_HISTORY:]:
                role_label = "You" if msg.role == "user" else "Assistant"
                context_parts.append(f"{role_label}: {msg.content}")
            context_parts.append("")

        # Add current message
        context_parts.append("# Current Message:")
        context_parts.append(f"You: {current_message}")

        context = "\n".join(context_parts)
        return context, memory_ids

    async def generate_response_stream(
        self, session_id: str, user_id: str, message_content: str, include_obsidian_memory: bool = False
    ) -> AsyncGenerator[Dict, None]:
        """Generate streaming response with memory context."""
        if not self._initialized:
            await self.initialize()

        try:
            # Save user message
            user_message = ChatMessage(
                message_id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                role="user",
                content=message_content
            )
            await self.add_message(user_message)

            # Format context with memories
            context, memory_ids = await self.format_conversation_context(
                session_id, user_id, message_content, include_obsidian_memory=include_obsidian_memory
            )

            # Send memory context used
            yield {
                "type": "memory_context",
                "data": {
                    "memory_ids": memory_ids,
                    "memory_count": len(memory_ids)
                },
                "timestamp": time.time()
            }

            # Create system prompt
            system_prompt = """You are a helpful AI assistant with access to the user's personal memories and conversation history. 

Use the provided memories and conversation context to give personalized, contextual responses. If memories are relevant, reference them naturally in your response. Be conversational and helpful.

If no relevant memories are available, respond normally based on the conversation context."""

            # Prepare full prompt
            full_prompt = f"{system_prompt}\n\n{context}"

            # Generate streaming response
            logger.info(f"Generating response for session {session_id} with {len(memory_ids)} memories")
            
            # Note: For now, we'll use the regular generate method
            # In the future, this should be replaced with actual streaming
            response_content = self.llm_client.generate(
                prompt=full_prompt
            )

            # Simulate streaming by yielding chunks
            words = response_content.split()
            current_text = ""
            
            for i, word in enumerate(words):
                current_text += word + " "
                
                # Yield every few words to simulate streaming
                if i % 3 == 0 or i == len(words) - 1:
                    yield {
                        "type": "token",
                        "data": current_text.strip(),
                        "timestamp": time.time()
                    }
                    await asyncio.sleep(0.05)  # Small delay for realistic streaming

            # Save assistant message
            assistant_message = ChatMessage(
                message_id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                role="assistant",
                content=response_content.strip(),
                memories_used=memory_ids
            )
            await self.add_message(assistant_message)

            # Send completion signal
            yield {
                "type": "complete",
                "data": {
                    "message_id": assistant_message.message_id,
                    "memories_used": memory_ids
                },
                "timestamp": time.time()
            }

        except Exception as e:
            logger.error(f"Error generating response for session {session_id}: {e}")
            yield {
                "type": "error",
                "data": {"error": str(e)},
                "timestamp": time.time()
            }

    async def update_session_title(self, session_id: str, user_id: str, title: str) -> bool:
        """Update a session's title."""
        if not self._initialized:
            await self.initialize()

        try:
            result = await self.sessions_collection.update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": {"title": title, "updated_at": datetime.utcnow()}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update session title: {e}")
            return False

    async def get_chat_statistics(self, user_id: str) -> Dict:
        """Get chat statistics for a user."""
        if not self._initialized:
            await self.initialize()

        try:
            # Count sessions
            session_count = await self.sessions_collection.count_documents({"user_id": user_id})
            
            # Count messages
            message_count = await self.messages_collection.count_documents({"user_id": user_id})
            
            # Get most recent session
            latest_session = await self.sessions_collection.find_one(
                {"user_id": user_id},
                sort=[("updated_at", -1)]
            )
            
            return {
                "total_sessions": session_count,
                "total_messages": message_count,
                "last_chat": latest_session["updated_at"] if latest_session else None
            }
        except Exception as e:
            logger.error(f"Failed to get chat statistics for user {user_id}: {e}")
            return {"total_sessions": 0, "total_messages": 0, "last_chat": None}

    async def extract_memories_from_session(self, session_id: str, user_id: str) -> Tuple[bool, List[str], int]:
        """Extract and store memories from a chat session.
        
        Args:
            session_id: ID of the chat session to extract memories from
            user_id: User ID for authorization and memory scoping
            
        Returns:
            Tuple of (success: bool, memory_ids: List[str], memory_count: int)
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Verify session belongs to user
            session = await self.sessions_collection.find_one({
                "session_id": session_id,
                "user_id": user_id
            })
            
            if not session:
                logger.error(f"Session {session_id} not found for user {user_id}")
                return False, [], 0

            # Get all messages from the session
            messages = await self.get_session_messages(session_id, user_id)
            
            if not messages or len(messages) < 2:  # Need at least user + assistant message
                logger.info(f"Not enough messages in session {session_id} for memory extraction")
                return True, [], 0

            # Format messages as a transcript
            transcript_parts = []
            for message in messages:
                role = "User" if message.role == "user" else "Assistant"
                transcript_parts.append(f"{role}: {message.content}")
            
            transcript = "\n".join(transcript_parts)
            
            # Get user email for memory service
            user_email = session.get("user_email", f"user_{user_id}")
            
            # Extract memories using the memory service
            success, memory_ids = await self.memory_service.add_memory(
                transcript=transcript,
                client_id="chat_interface",
                source_id=f"chat_{session_id}",
                user_id=user_id,
                user_email=user_email,
                allow_update=True  # Allow deduplication and updates
            )
            
            if success:
                logger.info(f"✅ Extracted {len(memory_ids)} memories from chat session {session_id}")
                return True, memory_ids, len(memory_ids)
            else:
                logger.error(f"❌ Failed to extract memories from chat session {session_id}")
                return False, [], 0
                
        except Exception as e:
            logger.error(f"Failed to extract memories from session {session_id}: {e}")
            return False, [], 0


# Global service instance
_chat_service = None


def get_chat_service() -> ChatService:
    """Get the global chat service instance."""
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service


async def cleanup_chat_service():
    """Cleanup chat service resources."""
    global _chat_service
    if _chat_service:
        _chat_service._initialized = False
        _chat_service = None
        logger.info("Chat service cleaned up")
