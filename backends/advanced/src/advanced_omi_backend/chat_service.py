"""
Chat service implementation for Chronicle with memory integration.

This module provides:
- Chat session management with MongoDB persistence
- Memory-enhanced RAG for contextual responses
- Streaming LLM responses with proper error handling
- Integration with existing mem0 memory infrastructure
"""

import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorCollection

from advanced_omi_backend.database import get_database
from advanced_omi_backend.llm_client import async_chat_with_tools, get_llm_client
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.user import get_user_by_id
from advanced_omi_backend.observability.otel_setup import (
    get_tracer,
    is_otel_enabled,
    set_otel_session,
    set_trace_io,
)
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.prompt_registry import get_prompt_registry
from advanced_omi_backend.services.knowledge_graph.kb import KnowledgeBaseManager
from advanced_omi_backend.services.memory import get_memory_service
from advanced_omi_backend.services.memory.base import MemoryEntry
from advanced_omi_backend.services.obsidian_service import (
    ObsidianSearchError,
    get_obsidian_service,
)
from advanced_omi_backend.services.plugin_service import dispatch_plugin_event

logger = logging.getLogger(__name__)

# Configuration
MAX_MEMORY_CONTEXT = 5  # Maximum number of memories to include in context
MAX_CONVERSATION_HISTORY = 10  # Maximum conversation turns to keep in context
MAX_TOOL_ROUNDS = 5  # Maximum tool-calling rounds in tool mode

MEMORY_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memories",
        "description": (
            "Search the user's personal memory database for relevant information. "
            "Use when the question might benefit from personal context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for finding relevant memories",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 20)",
                },
            },
            "required": ["query"],
        },
    },
}


class ChatMessage:
    """Represents a chat message."""

    def __init__(
        self,
        message_id: str,
        session_id: str,
        user_id: str,
        role: str,  # 'user' or 'assistant'
        content: str,
        timestamp: Optional[datetime] = None,
        memories_used: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ):
        self.message_id = message_id
        self.session_id = session_id
        self.user_id = user_id
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.now(timezone.utc)
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
        title: Optional[str] = None,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
        metadata: Optional[Dict] = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.title = title or "New Chat"
        self.created_at = created_at or datetime.now(timezone.utc)
        self.updated_at = updated_at or datetime.now(timezone.utc)
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
        self._kb = KnowledgeBaseManager()

    async def _get_system_prompt(self) -> str:
        """
        Get system prompt from config with fallback to prompt registry default.

        Returns:
            str: System prompt for chat interactions
        """
        try:
            reg = get_models_registry()
            if reg and hasattr(reg, "chat"):
                chat_config = reg.chat
                prompt = chat_config.get("system_prompt")
                if prompt:
                    logger.info(
                        f"✅ Loaded chat system prompt from config (length: {len(prompt)} chars)"
                    )
                    logger.debug(f"System prompt: {prompt[:100]}...")
                    return prompt
        except Exception as e:
            logger.warning(f"Failed to load chat system prompt from config: {e}")

        # Fallback to prompt registry
        try:
            registry = get_prompt_registry()
            prompt = await registry.get_prompt("chat.system")
            logger.info("Using chat system prompt from prompt registry")
            return prompt
        except Exception as e:
            logger.warning(f"Failed to load chat system prompt from registry: {e}")

        # Final fallback
        logger.info("Using hardcoded default chat system prompt")
        return """You are a helpful AI assistant with access to the user's personal memories and conversation history.

Use the provided memories and conversation context to give personalized, contextual responses. If memories are relevant, reference them naturally in your response. Be conversational and helpful.

If no relevant memories are available, respond normally based on the conversation context."""

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
            await self.sessions_collection.create_index(
                [("user_id", 1), ("updated_at", -1)]
            )
            await self.messages_collection.create_index(
                [("session_id", 1), ("timestamp", 1)]
            )
            await self.messages_collection.create_index(
                [("user_id", 1), ("timestamp", -1)]
            )

            # Initialize LLM client and memory service
            self.llm_client = get_llm_client()
            self.memory_service = get_memory_service()

            self._initialized = True
            logger.info("Chat service initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize chat service: {e}")
            raise

    async def create_session(
        self, user_id: str, title: Optional[str] = None
    ) -> ChatSession:
        """Create a new chat session."""
        if not self._initialized:
            await self.initialize()

        session = ChatSession(
            session_id=str(uuid4()), user_id=user_id, title=title or "New Chat"
        )

        await self.sessions_collection.insert_one(session.to_dict())
        logger.info(f"Created new chat session {session.session_id} for user {user_id}")
        return session

    async def get_user_sessions(
        self, user_id: str, limit: int = 50
    ) -> List[ChatSession]:
        """Get all chat sessions for a user."""
        if not self._initialized:
            await self.initialize()

        cursor = (
            self.sessions_collection.find({"user_id": user_id})
            .sort("updated_at", -1)
            .limit(limit)
        )

        sessions = []
        async for doc in cursor:
            sessions.append(ChatSession.from_dict(doc))

        return sessions

    async def get_session(self, session_id: str, user_id: str) -> Optional[ChatSession]:
        """Get a specific chat session."""
        if not self._initialized:
            await self.initialize()

        doc = await self.sessions_collection.find_one(
            {"session_id": session_id, "user_id": user_id}
        )

        if doc:
            return ChatSession.from_dict(doc)
        return None

    async def delete_session(self, session_id: str, user_id: str) -> bool:
        """Delete a chat session and all its messages."""
        if not self._initialized:
            await self.initialize()

        # Delete all messages in the session
        await self.messages_collection.delete_many(
            {"session_id": session_id, "user_id": user_id}
        )

        # Delete the session
        result = await self.sessions_collection.delete_one(
            {"session_id": session_id, "user_id": user_id}
        )

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

        cursor = (
            self.messages_collection.find(
                {"session_id": session_id, "user_id": user_id}
            )
            .sort("timestamp", 1)
            .limit(limit)
        )

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
                {"$set": update_data},
            )

            return True
        except Exception as e:
            logger.error(f"Failed to add message to session {message.session_id}: {e}")
            return False

    async def get_relevant_memories(
        self, query: str, user_id: str, limit: Optional[int] = None
    ) -> List[MemoryEntry]:
        """Get relevant memories for the user's query."""
        try:
            memory_limit = limit if limit is not None else MAX_MEMORY_CONTEXT
            memories = await self.memory_service.search_memories(
                query=query, user_id=user_id, limit=memory_limit
            )
            logger.info(
                f"Retrieved {len(memories)} relevant memories for query: {query[:50]}..."
            )
            return memories
        except Exception as e:
            logger.error(f"Failed to retrieve memories for user {user_id}: {e}")
            return []

    async def format_conversation_context(
        self,
        session_id: str,
        user_id: str,
        current_message: str,
        include_obsidian_memory: bool = False,
        memory_limit: Optional[int] = None,
    ) -> Tuple[str, List[str]]:
        """Format conversation context with memory integration."""
        # Get recent conversation history
        messages = await self.get_session_messages(
            session_id, user_id, MAX_CONVERSATION_HISTORY
        )

        # Get relevant memories
        memories = await self.get_relevant_memories(
            current_message, user_id, limit=memory_limit
        )
        memory_ids = [memory.id for memory in memories if memory.id]

        # Build context string
        context_parts = []

        # Add basic memory (user's MEMORY.md) if available
        basic_memory = self._kb.get_basic_memory(user_id)
        if basic_memory:
            context_parts.append("# User Knowledge Base:")
            context_parts.append(basic_memory)
            context_parts.append("")

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
                obsidian_result = await obsidian_service.search_obsidian(
                    current_message, user_id
                )
                obsidian_context = obsidian_result["results"]
                if obsidian_context:
                    context_parts.append("# Relevant Obsidian Notes:")
                    for entry in obsidian_context:
                        context_parts.append(entry)
                    context_parts.append("")
                    logger.info(
                        f"Added {len(obsidian_context)} Obsidian notes to context"
                    )
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

    async def _get_tool_mode_system_prompt(self) -> str:
        """Get system prompt for tool-based memory mode."""
        try:
            registry = get_prompt_registry()
            prompt = await registry.get_prompt("chat.system.tool_mode")
            logger.info("Using tool-mode chat system prompt from prompt registry")
            return prompt
        except Exception:
            pass

        return (
            "You are a helpful AI assistant. You have access to a tool called "
            "`search_memories` that searches the user's personal memory database.\n\n"
            "Use the tool when the user's question might benefit from personal context "
            "(e.g., preferences, past events, people they know, things they've said before). "
            "Do NOT use the tool for general knowledge questions, greetings, or simple tasks "
            "like math.\n\n"
            "When memories are returned, weave them naturally into your response without "
            "listing them mechanically."
        )

    async def _generate_response_tool_mode(
        self,
        session_id: str,
        user_id: str,
        message_content: str,
        memory_limit: Optional[int] = None,
    ) -> AsyncGenerator[Dict, None]:
        """Generate response using tool-based memory retrieval (LLM decides when to search)."""
        if not self._initialized:
            await self.initialize()

        try:
            # Save user message
            user_message = ChatMessage(
                message_id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                role="user",
                content=message_content,
            )
            await self.add_message(user_message)

            # Build messages list with proper message objects
            system_prompt = await self._get_tool_mode_system_prompt()

            # Inject basic memory into system prompt
            basic_memory = self._kb.get_basic_memory(user_id)
            if basic_memory:
                system_prompt += f"\n\n# User Knowledge Base:\n{basic_memory}"

            messages = [{"role": "system", "content": system_prompt}]

            # Add conversation history
            history = await self.get_session_messages(
                session_id, user_id, MAX_CONVERSATION_HISTORY
            )
            for msg in history:
                # Skip the message we just saved (it's the current one)
                if msg.message_id == user_message.message_id:
                    continue
                messages.append({"role": msg.role, "content": msg.content})

            # Add current user message
            messages.append({"role": "user", "content": message_content})

            all_memory_ids = []

            # Tool-calling loop
            for _ in range(MAX_TOOL_ROUNDS):
                response = await async_chat_with_tools(
                    messages,
                    tools=[MEMORY_SEARCH_TOOL],
                    operation="chat",
                )
                choice = response.choices[0]

                if choice.finish_reason == "tool_calls" or choice.message.tool_calls:
                    # Append assistant message with tool calls
                    assistant_msg = choice.message.model_dump()
                    messages.append(assistant_msg)

                    for tool_call in choice.message.tool_calls:
                        fn_name = tool_call.function.name
                        try:
                            fn_args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError:
                            fn_args = {}

                        if fn_name == "search_memories":
                            query = fn_args.get("query", message_content)
                            limit = min(fn_args.get("limit", 5), 20)
                            if memory_limit is not None:
                                limit = min(limit, memory_limit)

                            memories = await self.get_relevant_memories(
                                query, user_id, limit=limit
                            )
                            memory_ids = [m.id for m in memories if m.id]
                            all_memory_ids.extend(memory_ids)

                            result = [
                                {"content": m.content, "id": m.id}
                                for m in memories
                                if m.content
                            ]
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": json.dumps(result, default=str),
                                }
                            )
                        else:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": json.dumps(
                                        {"error": f"Unknown tool: {fn_name}"}
                                    ),
                                }
                            )
                    continue

                # Plain text response — done
                response_content = (choice.message.content or "").strip()

                # Deduplicate memory IDs
                unique_memory_ids = list(dict.fromkeys(all_memory_ids))

                yield {
                    "type": "memory_context",
                    "data": {
                        "memory_ids": unique_memory_ids,
                        "memory_count": len(unique_memory_ids),
                    },
                    "timestamp": time.time(),
                }

                yield {
                    "type": "token",
                    "data": response_content,
                    "timestamp": time.time(),
                }

                # Save assistant message
                assistant_message = ChatMessage(
                    message_id=str(uuid4()),
                    session_id=session_id,
                    user_id=user_id,
                    role="assistant",
                    content=response_content,
                    memories_used=unique_memory_ids,
                )
                await self.add_message(assistant_message)

                set_trace_io(output={"response": response_content})

                yield {
                    "type": "complete",
                    "data": {
                        "message_id": assistant_message.message_id,
                        "memories_used": unique_memory_ids,
                    },
                    "timestamp": time.time(),
                }
                return

            # Exhausted tool rounds without a text response
            logger.warning(
                f"Tool mode exhausted {MAX_TOOL_ROUNDS} rounds for session {session_id}"
            )
            yield {
                "type": "memory_context",
                "data": {"memory_ids": [], "memory_count": 0},
                "timestamp": time.time(),
            }
            yield {
                "type": "token",
                "data": "I'm sorry, I wasn't able to formulate a response. Please try again.",
                "timestamp": time.time(),
            }
            yield {"type": "complete", "data": {}, "timestamp": time.time()}

        except Exception as e:
            logger.error(f"Error in tool-mode response for session {session_id}: {e}")
            yield {
                "type": "error",
                "data": {"error": str(e)},
                "timestamp": time.time(),
            }

    async def generate_response_stream(
        self,
        session_id: str,
        user_id: str,
        message_content: str,
        include_obsidian_memory: bool = False,
        memory_limit: Optional[int] = None,
        memory_mode: str = "always",
    ) -> AsyncGenerator[Dict, None]:
        """Generate streaming response with memory context."""
        set_otel_session(session_id)

        tracer = get_tracer() if is_otel_enabled() else None
        span_ctx = (
            tracer.start_as_current_span(
                "chat",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.conversation.id": session_id,
                    "chronicle.user_id": user_id,
                    "langfuse.user.id": user_id,
                    "chronicle.pipeline.stage": "chat",
                },
            )
            if tracer
            else contextlib.nullcontext()
        )

        with span_ctx:
            set_trace_io(input={"message": message_content})

            if memory_mode == "tool":
                async for event in self._generate_response_tool_mode(
                    session_id=session_id,
                    user_id=user_id,
                    message_content=message_content,
                    memory_limit=memory_limit,
                ):
                    yield event
                return

            if not self._initialized:
                await self.initialize()

            try:
                # Save user message
                user_message = ChatMessage(
                    message_id=str(uuid4()),
                    session_id=session_id,
                    user_id=user_id,
                    role="user",
                    content=message_content,
                )
                await self.add_message(user_message)

                if memory_mode == "off":
                    # No memory search — just conversation history
                    messages = await self.get_session_messages(
                        session_id, user_id, MAX_CONVERSATION_HISTORY
                    )
                    context_parts = []
                    if messages:
                        context_parts.append("# Recent Conversation:")
                        for msg in messages[-MAX_CONVERSATION_HISTORY:]:
                            role_label = "You" if msg.role == "user" else "Assistant"
                            context_parts.append(f"{role_label}: {msg.content}")
                        context_parts.append("")
                    context_parts.append("# Current Message:")
                    context_parts.append(f"You: {message_content}")
                    context = "\n".join(context_parts)
                    memory_ids = []
                else:
                    # Format context with memories (always mode)
                    context, memory_ids = await self.format_conversation_context(
                        session_id,
                        user_id,
                        message_content,
                        include_obsidian_memory=include_obsidian_memory,
                        memory_limit=memory_limit,
                    )

                # Send memory context used
                yield {
                    "type": "memory_context",
                    "data": {"memory_ids": memory_ids, "memory_count": len(memory_ids)},
                    "timestamp": time.time(),
                }

                # Get system prompt from config
                system_prompt = await self._get_system_prompt()

                # Prepare full prompt
                full_prompt = f"{system_prompt}\n\n{context}"

                # Generate streaming response
                logger.info(
                    f"Generating response for session {session_id} with {len(memory_ids)} memories"
                )

                # Resolve chat operation temperature from config
                chat_temp = None
                registry = get_models_registry()
                if registry:
                    chat_op = registry.get_llm_operation("chat")
                    chat_temp = chat_op.temperature

                response_content = self.llm_client.generate(
                    prompt=full_prompt,
                    temperature=chat_temp,
                )

                yield {
                    "type": "token",
                    "data": response_content.strip(),
                    "timestamp": time.time(),
                }

                # Save assistant message
                assistant_message = ChatMessage(
                    message_id=str(uuid4()),
                    session_id=session_id,
                    user_id=user_id,
                    role="assistant",
                    content=response_content.strip(),
                    memories_used=memory_ids,
                )
                await self.add_message(assistant_message)

                set_trace_io(output={"response": response_content.strip()})

                # Send completion signal
                yield {
                    "type": "complete",
                    "data": {
                        "message_id": assistant_message.message_id,
                        "memories_used": memory_ids,
                    },
                    "timestamp": time.time(),
                }

            except Exception as e:
                logger.error(f"Error generating response for session {session_id}: {e}")
                yield {
                    "type": "error",
                    "data": {"error": str(e)},
                    "timestamp": time.time(),
                }

    async def update_session_title(
        self, session_id: str, user_id: str, title: str
    ) -> bool:
        """Update a session's title."""
        if not self._initialized:
            await self.initialize()

        try:
            result = await self.sessions_collection.update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
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
            session_count = await self.sessions_collection.count_documents(
                {"user_id": user_id}
            )

            # Count messages
            message_count = await self.messages_collection.count_documents(
                {"user_id": user_id}
            )

            # Get most recent session
            latest_session = await self.sessions_collection.find_one(
                {"user_id": user_id}, sort=[("updated_at", -1)]
            )

            return {
                "total_sessions": session_count,
                "total_messages": message_count,
                "last_chat": latest_session["updated_at"] if latest_session else None,
            }
        except Exception as e:
            logger.error(f"Failed to get chat statistics for user {user_id}: {e}")
            return {"total_sessions": 0, "total_messages": 0, "last_chat": None}

    async def extract_memories_from_session(
        self, session_id: str, user_id: str
    ) -> Tuple[bool, List[str], int]:
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
            session = await self.sessions_collection.find_one(
                {"session_id": session_id, "user_id": user_id}
            )

            if not session:
                logger.error(f"Session {session_id} not found for user {user_id}")
                return False, [], 0

            # Get all messages from the session
            messages = await self.get_session_messages(session_id, user_id)

            if (
                not messages or len(messages) < 2
            ):  # Need at least user + assistant message
                logger.info(
                    f"Not enough messages in session {session_id} for memory extraction"
                )
                return True, [], 0

            # Resolve speaker labels from the user's profile so extracted memories
            # are attributed to the actual person instead of a generic "User".
            user = await get_user_by_id(user_id)
            user_label = user.display_name if user and user.display_name else "User"
            assistant_label = (
                user.assistant_name if user and user.assistant_name else "Assistant"
            )

            # Format messages as a transcript
            transcript_parts = []
            for message in messages:
                role = user_label if message.role == "user" else assistant_label
                transcript_parts.append(f"{role}: {message.content}")

            transcript = "\n".join(transcript_parts)

            # Get user email for memory service
            user_email = session.get("user_email", f"user_{user_id}")
            source_id = f"chat_{session_id}"

            success, memory_ids = await self.memory_service.add_memory(
                transcript=transcript,
                client_id="chat_interface",
                source_id=source_id,
                user_id=user_id,
                user_email=user_email,
                allow_update=True,
            )

            if success:
                logger.info(
                    f"✅ Extracted {len(memory_ids)} memories from chat session {session_id}"
                )
                memory_count = len(memory_ids or [])

                # Plugin dispatch — non-fatal, mirrors memory_jobs.py:398-422
                try:
                    memory_provider = getattr(
                        self.memory_service, "provider_identifier", "unknown"
                    )
                    await dispatch_plugin_event(
                        event=PluginEvent.MEMORY_PROCESSED,
                        user_id=user_id,
                        data={
                            "memories": memory_ids or [],
                            "conversation": {
                                "conversation_id": source_id,
                                "client_id": "chat_interface",
                                "user_id": user_id,
                                "user_email": user_email,
                            },
                            "memory_count": memory_count,
                            "conversation_id": source_id,
                        },
                        metadata={"memory_provider": memory_provider},
                        description=f"chat={session_id[:12]}, memories={memory_count}",
                    )
                except Exception as e:
                    logger.warning(
                        f"⚠️ Error triggering memory-level plugins for chat {session_id}: {e}"
                    )

                return True, memory_ids, len(memory_ids)
            else:
                logger.error(
                    f"❌ Failed to extract memories from chat session {session_id}"
                )
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
