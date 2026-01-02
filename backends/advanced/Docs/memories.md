# Memory Service Configuration and Customization

> 📖 **Prerequisite**: Read [quickstart.md](./quickstart.md) first for system overview.

This document explains how to configure and customize the memory service in the chronicle backend.

**Code References**: 
- **Main Implementation**: `src/memory/memory_service.py`
- **Event Coordination**: `src/advanced_omi_backend/transcript_coordinator.py` (zero-polling async events)
- **Repository Layer**: `src/advanced_omi_backend/conversation_repository.py` (clean data access)
- **Processing Manager**: `src/advanced_omi_backend/processors.py` (MemoryProcessor class)
- **Conversation Management**: `src/advanced_omi_backend/conversation_manager.py` (lifecycle coordination)
- **Configuration**: `config/config.yml` (memory section) + `src/model_registry.py`

## Overview

The memory service uses [Mem0](https://mem0.ai/) to store, retrieve, and search conversation memories. It integrates with Ollama for embeddings and LLM processing, and Qdrant for vector storage.

**Key Architecture Changes**: 
1. **Event-Driven Processing**: Memories use asyncio events instead of polling/retry mechanisms
2. **Repository Pattern**: Clean data access through ConversationRepository
3. **User-Centric Storage**: All memories keyed by user_id instead of client_id
4. **Single Source of Truth**: MongoDB as the only transcript storage (no duplicates)

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Audio Stream  │    │ TranscriptCoord  │    │ ConversationRepo│
│   (WebSocket)   │───▶│  (Event-Driven)  │───▶│ (MongoDB Access)│
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │                          │
                              ▼                          ▼
                    ┌──────────────────┐    ┌──────────────────┐
                    │ MemoryProcessor  │    │   Conversation   │
                    │ (Event-Waiting)  │◄───│   Manager        │
                    └──────────────────┘    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   Ollama + Mem0  │───▶ ┌─────────────────┐
                    │ (LLM + Embeddings)│    │    Qdrant       │
                    └──────────────────┘    │ (Vector Store)  │
                                            │  (user_id keyed)│
                                            └─────────────────┘
```

**Key Flow:**
1. **Audio** → **TranscriptCoordinator** signals completion
2. **ConversationManager** waits for event, queues memory processing  
3. **MemoryProcessor** uses **ConversationRepository** for data access
4. **Mem0 + Ollama** extract and store memories in **Qdrant**

## Configuration

### Environment Variables

The memory service is configured via environment variables:

```bash
# Ollama Configuration
OLLAMA_BASE_URL=http://192.168.0.110:11434

# Qdrant Configuration (optional)
QDRANT_BASE_URL=localhost

# Mem0 Organization Settings (optional)
MEM0_ORGANIZATION_ID=chronicle-org
MEM0_PROJECT_ID=audio-conversations
MEM0_APP_ID=omi-backend

# Disable telemetry (privacy)
MEM0_TELEMETRY=False
```

### Memory Service Configuration

The core configuration is in `src/memory/memory_service.py:45-81`:

```python
MEM0_CONFIG = {
    "llm": {
        "provider": "ollama",
        "config": {
            "model": "llama3.1:latest",
            "ollama_base_url": OLLAMA_BASE_URL,
            "temperature": 0,
        },
    },
    "embedder": {
        "provider": "ollama", 
        "config": {
            "model": "nomic-embed-text:latest",
            "embedding_dims": 768,
            "ollama_base_url": OLLAMA_BASE_URL,
        },
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "omi_memories",
            "embedding_model_dims": 768,
            "host": QDRANT_BASE_URL,
            "port": 6333,
        },
    },
}
```

## Mem0 Custom Prompts Configuration

### Understanding Mem0 Prompts

Mem0 uses two types of custom prompts:

1. **`custom_fact_extraction_prompt`**: Controls how facts are extracted from conversations
2. **`custom_update_memory_prompt`**: Controls how memories are updated/merged

### Key Discovery: Fact Extraction Format

The `custom_fact_extraction_prompt` must follow a specific JSON format with few-shot examples:

```python
custom_fact_extraction_prompt = """
Please extract relevant facts from the conversation.
Here are some few shot examples:

Input: Hi.
Output: {"facts" : []}

Input: I need to buy groceries tomorrow.
Output: {"facts" : ["Need to buy groceries tomorrow"]}

Input: The meeting is at 3 PM on Friday.
Output: {"facts" : ["Meeting scheduled for 3 PM on Friday"]}

Now extract facts from the following conversation. Return only JSON format with "facts" key.
"""
```

### Configuration Parameters

Mem0 configuration requires these specific parameters:

- `custom_fact_extraction_prompt`: For fact extraction (if enabled)
- `version`: Should be set to "v1.1"
- Standard LLM, embedder, and vector_store configurations

### Common Issues

1. **Using `custom_prompt` instead of `custom_fact_extraction_prompt`**: Will cause empty results
2. **Missing JSON format examples**: Facts won't be extracted properly
3. **Setting `custom_fact_extraction_prompt` to empty string**: Disables fact extraction entirely

## Customization Options

### 1. LLM Model Configuration

#### Change the LLM Model

To use a different Ollama model for memory processing:

```python
# In memory_service.py
MEM0_CONFIG["llm"]["config"]["model"] = "llama3.2:latest"  # or any other model
```

#### Switch to OpenAI GPT-4o (Recommended for JSON Reliability)

For better JSON parsing and reduced errors, switch to OpenAI:

```bash
# In your .env file
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-5-mini  # Recommended for reliable JSON output

# Alternative models
# OPENAI_MODEL=gpt-5-mini  # Faster, cheaper option
# OPENAI_MODEL=gpt-3.5-turbo  # Budget option
```

Or configure via `config/config.yml` (memory block):

```yaml
memory_extraction:
  llm_settings:
    model: "gpt-5-mini"  # When LLM_PROVIDER=openai
    temperature: 0.1


fact_extraction:
  enabled: true  # Safe to enable with GPT-4o
  llm_settings:
    model: "gpt-5-mini"
    temperature: 0.0
```

#### Adjust LLM Parameters

```python
MEM0_CONFIG["llm"]["config"].update({
    "temperature": 0.1,      # Higher for more creative summaries
    "top_p": 0.9,           # Nucleus sampling
})
```

#### Benefits of OpenAI GPT-4o

**Improved JSON Reliability:**
- Consistent JSON formatting reduces parsing errors
- Better instruction following for structured output
- Built-in understanding of JSON requirements
- Reduced need to disable fact extraction

**When to Use GPT-4o:**
- Experiencing frequent JSON parsing errors
- Want to enable fact extraction safely
- Require consistent structured output

**Monitoring JSON Success:**
```bash
# Check for parsing errors
docker logs advanced-backend | grep "JSONDecodeError"

# Verify OpenAI usage
docker logs advanced-backend | grep "Using OpenAI provider"

docker logs advanced-backend | grep "OpenAI response"
```

### 2. Embedding Model Configuration

#### Change Embedding Model

```python
MEM0_CONFIG["embedder"]["config"]["model"] = "mxbai-embed-large:latest"
```

#### Adjust Embedding Dimensions

```python
# Must match your embedding model's output dimensions
MEM0_CONFIG["embedder"]["config"]["embedding_dims"] = 1024
MEM0_CONFIG["vector_store"]["config"]["embedding_model_dims"] = 1024
```

### 3. Memory Processing Customization

#### Custom Memory Prompt

You can customize how memories are extracted from conversations:

```python
# In src/memory/memory_service.py:207-225 (_add_memory_to_store function)
process_memory.add(
    transcript,
    user_id=user_id,  # Database user_id (not client_id)
    metadata={
        "client_id": client_id,  # Stored in metadata
        "user_email": user_email,
        # ... other metadata
    },
    prompt="Please extract key information and relationships from this conversation"
)
```

#### Memory Metadata

Enrich memories with custom metadata:

```python
metadata = {
    "source": "offline_streaming",
    "client_id": client_id,          # Client ID stored in metadata
    "user_email": user_email,        # User email for identification
    "audio_uuid": audio_uuid,
    "timestamp": int(time.time()),
    "conversation_context": "audio_transcription",
    "device_type": "audio_recording",
    "mood": "professional",          # Custom field
    "topics": ["sales", "meetings"], # Custom field
    "organization_id": MEM0_ORGANIZATION_ID,
    "project_id": MEM0_PROJECT_ID,
    "app_id": MEM0_APP_ID,
}
```

### 4. Vector Store Configuration

#### Change Collection Name

```python
MEM0_CONFIG["vector_store"]["config"]["collection_name"] = "my_custom_memories"
```

#### Qdrant Advanced Configuration

```python
MEM0_CONFIG["vector_store"]["config"].update({
    "url": "http://localhost:6333",  # Full URL
    "api_key": "your-api-key",       # If using Qdrant Cloud
    "prefer_grpc": True,             # Use gRPC instead of HTTP
})
```

### 5. Search and Retrieval Customization

#### Custom Search Filters

```python
def search_memories_with_filters(self, query: str, user_id: str, topic: str = None):
    filters = {}
    
    if topic:
        filters["metadata.topics"] = {"$in": [topic]}
    
    return self.memory.search(
        query=query,
        user_id=user_id,
        filters=filters,
        limit=20
    )
```

#### Memory Ranking

```python
def get_important_memories(self, user_id: str):
    """Get memories sorted by importance/frequency"""
    memories = self.memory.get_all(user_id=user_id)
    
    # Custom scoring logic
    for memory in memories:
        score = 0
        if "meeting" in memory.get('memory', '').lower():
            score += 2
        if "deadline" in memory.get('memory', '').lower():
            score += 3
        memory['importance_score'] = score
    
    return sorted(memories, key=lambda x: x.get('importance_score', 0), reverse=True)
```

## User-Centric Memory Architecture

### Key Changes

**All memories are now keyed by database user_id instead of client_id:**

- **Memory Storage**: `user_id` parameter identifies the memory owner
- **Client Information**: Stored in metadata for reference and debugging
- **User Email**: Included in metadata for easy identification
- **Backward Compatibility**: Admin debug shows both user and client information

### Client-User Mapping

The system maintains a mapping between client IDs and database users:

```python
# Client ID format: objectid_suffix-device_name
client_id = "cd7994-laptop"  # Maps to user_id="507f1f77bcf86cd799439011" (ObjectId)

# Memory storage uses database user_id (full ObjectId)
process_memory.add(
    transcript,
    user_id="507f1f77bcf86cd799439011",  # Database user_id (MongoDB ObjectId)
    metadata={
        "client_id": "cd7994-laptop",  # Client reference
        "user_email": "user@example.com",
        # ... other metadata
    }
)
```

## Memory Types and Structure

### Standard Memory Structure

```json
{
    "id": "01b76e66-8a9c-4567-b890-123456789abc",
    "memory": "Planning a vacation to Italy in September",
    "user_id": "abc123",
    "created_at": "2025-07-10T07:44:15.316499-07:00",
    "metadata": {
        "source": "offline_streaming",
        "client_id": "abc123-laptop",
        "user_email": "user@example.com",
        "audio_uuid": "test_audio_6e38c2c8",
        "timestamp": 1720616655,
        "conversation_context": "audio_transcription",
        "device_type": "audio_recording",
        "organization_id": "chronicle-org",
        "project_id": "audio-conversations",
        "app_id": "omi-backend"
    }
}
```


## Advanced Customization

### 1. Custom Memory Processing Pipeline

Create a custom processing function:

```python
def custom_memory_processor(transcript: str, client_id: str, audio_uuid: str, user_id: str, user_email: str):
    # Extract entities
    entities = extract_named_entities(transcript)
    
    # Classify conversation type
    conv_type = classify_conversation(transcript)
    
    # Generate custom summary
    summary = generate_custom_summary(transcript, conv_type)
    
    # Store with enriched metadata
    process_memory.add(
        summary,
        user_id=user_id,  # Database user_id
        metadata={
            "client_id": client_id,
            "user_email": user_email,
            "entities": entities,
            "conversation_type": conv_type,
            "audio_uuid": audio_uuid,
            "processing_version": "v2.0"
        }
    )
```

### 2. Multiple Memory Collections

Configure different collections for different types of memories:

```python
def init_specialized_memory_services():
    # Personal memories
    personal_config = MEM0_CONFIG.copy()
    personal_config["vector_store"]["config"]["collection_name"] = "personal_memories"
    
    # Work memories  
    work_config = MEM0_CONFIG.copy()
    work_config["vector_store"]["config"]["collection_name"] = "work_memories"
    work_config["custom_prompt"] = "Focus on work-related tasks, meetings, and projects"
    
    return {
        "personal": Memory.from_config(personal_config),
        "work": Memory.from_config(work_config)
    }
```

### 3. Memory Lifecycle Management

Implement automatic memory cleanup:

```python
def cleanup_old_memories(self, user_id: str, days_old: int = 365):
    """Remove memories older than specified days"""
    cutoff_timestamp = int(time.time()) - (days_old * 24 * 60 * 60)
    
    memories = self.get_all_memories(user_id)
    for memory in memories:
        if memory.get('metadata', {}).get('timestamp', 0) < cutoff_timestamp:
            self.delete_memory(memory['id'])
```

## Testing Memory Configuration

Use the provided test script to verify your configuration:

```bash
# Run the memory test script
python test_memory_creation.py
```

This will:
- Test connectivity to Ollama and Qdrant
- Create sample memories with database user IDs (not client IDs)
- Test memory retrieval and search functionality
- Verify the new user-centric memory structure and metadata
- Validate client-user mapping functionality

## Troubleshooting

### Common Issues

1. **Connection Timeouts**
   - Check Ollama is running: `curl http://localhost:11434/api/version`
   - Check Qdrant is accessible: `curl http://localhost:6333/collections`

2. **Memory Not Created**
   - Check Ollama has required models: `ollama list`
   - Verify Qdrant collection exists
   - Check memory service logs for errors

3. **Search Not Working**
   - Ensure embedding model is available in Ollama
   - Check vector dimensions match between embedder and Qdrant
   - Verify collection has vectors: `curl http://localhost:6333/collections/omi_memories`

### Required Ollama Models

Make sure these models are available:

```bash
# LLM for memory processing
ollama pull llama3.1:latest

# Embedding model for semantic search
ollama pull nomic-embed-text:latest
```

### Memory Service Logs

Enable debug logging to troubleshoot issues:

```python
import logging
logging.getLogger("memory_service").setLevel(logging.DEBUG)
```

## Performance Optimization

### 1. Batch Processing

Process multiple memories at once:

```python
async def batch_add_memories(self, transcripts_data: List[Dict]):
    tasks = []
    for data in transcripts_data:
        task = self.add_memory(
            data['transcript'], 
            data['client_id'], 
            data['audio_uuid'],
            data['user_id'],      # Database user_id
            data['user_email']    # User email
        )
        tasks.append(task)
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results
```

### 2. Memory Compression

Implement memory consolidation:

```python
def consolidate_memories(self, user_id: str, time_window_hours: int = 24):
    """Consolidate related memories from the same time period"""
    recent_memories = self.get_recent_memories(user_id, time_window_hours)
    
    if len(recent_memories) > 5:  # If many memories in short time
        consolidated = self.summarize_memories(recent_memories)
        
        # Delete individual memories and store consolidated version
        for memory in recent_memories:
            self.delete_memory(memory['id'])
        
        return self.add_consolidated_memory(consolidated, user_id)
```

## API Endpoints

The memory service exposes these endpoints with enhanced search capabilities:

- `GET /api/memories` - Get user memories with total count support (keyed by database user_id)
- `GET /api/memories/search?query={query}&limit={limit}` - **Semantic memory search** with relevance scoring (user-scoped)  
- `GET /api/memories/unfiltered` - User's memories without filtering for debugging
- `DELETE /api/memories/{memory_id}` - Delete specific memory (requires authentication)
- `GET /api/memories/admin` - Admin view of all memories across all users (superuser only)

### Enhanced Search Features

**Semantic Search (`/api/memories/search`)**:
- **Relevance Scoring**: Returns similarity scores from vector database (0.0-1.0 range)
- **Configurable Limits**: Supports `limit` parameter for result count control  
- **User Scoped**: Results automatically filtered by authenticated user
- **Vector-based**: Uses embeddings for contextual understanding beyond keyword matching

**Memory Count API**: 
- **Chronicle Provider**: Native Qdrant count API provides accurate total counts
- **OpenMemory MCP Provider**: Count support varies by OpenMemory implementation
- **Response Format**: `{"memories": [...], "total_count": 42}` when supported

### Admin Endpoints

#### All Memories Endpoint (`/api/memories/admin`)

Returns all memories across all users in a clean, searchable format:

```json
{
    "total_memories": 25,
    "total_users": 3,
    "memories": [
        {
            "id": "memory-uuid",
            "memory": "Planning vacation to Italy in September",
            "user_id": "abc123",
            "created_at": "2025-07-10T14:30:00Z",
            "owner_user_id": "abc123",
            "owner_email": "user@example.com", 
            "owner_display_name": "John Doe",
            "metadata": {
                "client_id": "abc123-laptop",
                "user_email": "user@example.com",
                "audio_uuid": "audio-uuid"
            }
        }
    ]
}
```

#### Admin Endpoint Details

The admin endpoint provides comprehensive memory information:

```json
{
    "total_users": 2,
    "total_memories": 15,
    "admin_user": {
        "id": "admin1",
        "email": "admin@example.com",
        "is_superuser": true
    },
    "users_with_memories": [
        {
            "user_id": "abc123",
            "email": "user@example.com",
            "memory_count": 10,
            "memories": [...],
            "registered_clients": [
                {
                    "client_id": "abc123-laptop",
                    "device_name": "laptop",
                    "last_seen": "2025-07-10T14:30:00Z"
                }
            ],
            "client_count": 1
        }
    ]
}
```

## Conclusion

The memory service is highly customizable and can be adapted for various use cases. Key areas for customization include:

- LLM and embedding models
- Memory processing prompts
- Metadata enrichment
- Search and retrieval logic
- Storage collections and structure

For more advanced use cases, consider implementing custom processing pipelines, multiple memory types, or integration with external knowledge bases.

## Migration from Client-Based to User-Based Storage

If migrating from an existing system where memories were keyed by client_id:

1. **Clean existing data**: Remove old memories from Qdrant
2. **Restart services**: Ensure new architecture is active
3. **Test with fresh data**: Verify memories are properly keyed by user_id
4. **Admin verification**: Use `/api/memories/admin` to confirm proper storage

The new architecture ensures proper user isolation and simplifies admin debugging while maintaining all client information in metadata.


Both load all user memories and view all memories are helpful
Both views complement each other - the debug view helps you understand how the system is working, while the clean view
helps you understand what content is being stored.
