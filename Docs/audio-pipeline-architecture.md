# Audio Pipeline Architecture

This document explains how audio flows through the Chronicle system from initial capture to final storage, including all intermediate processing stages, Redis streams, and data storage locations.

## Table of Contents

- [Overview](#overview)
- [Architecture Diagram](#architecture-diagram)
- [Data Sources](#data-sources)
- [Redis Streams: The Central Pipeline](#redis-streams-the-central-pipeline)
- [Producer: AudioStreamProducer](#producer-audiostreamproducer)
- [Dual-Consumer Architecture](#dual-consumer-architecture)
- [Transcription Results Aggregator](#transcription-results-aggregator)
- [Job Queue Orchestration (RQ)](#job-queue-orchestration-rq)
- [Data Storage](#data-storage)
- [Complete End-to-End Flow](#complete-end-to-end-flow)
- [Key Design Patterns](#key-design-patterns)
- [Failure Handling](#failure-handling)

## Overview

Chronicle's audio pipeline is built on three core technologies:

- **Redis Streams**: Distributed message queues for audio chunks and transcription results
- **Background Tasks**: Async consumers that process streams independently
- **RQ Job Queue**: Orchestrates session-level and conversation-level workflows

**Key Insight**: Multiple workers can independently consume the **same audio stream** using Redis Consumer Groups, enabling parallel processing paths (transcription + disk persistence) without duplication.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        AUDIO INPUT                              │
│  WebSocket (/ws) │ File Upload (/audio/upload) │ Google Drive  │
└────────────────────────────────┬────────────────────────────────┘
                                 ↓
                    ┌────────────────────────┐
                    │  AudioStreamProducer   │
                    │  - Chunk audio (0.25s) │
                    │  - Session metadata    │
                    └────────────┬───────────┘
                                 ↓
                    ┌────────────────────────────────┐
                    │  Redis Stream (Per Client)     │
                    │  audio:stream:{client_id}      │
                    └─────┬──────────────────┬───────┘
                          ↓                  ↓
          ┌───────────────────────┐  ┌──────────────────────┐
          │ Transcription Consumer│  │ Audio Persistence    │
          │ Group (streaming/batch)│  │ Consumer Group       │
          │                       │  │                      │
          │ → Deepgram WebSocket  │  │ → Writes WAV files   │
          │ → Batch buffering     │  │ → Monitors rotation  │
          │ → Publish results     │  │ → Stores file paths  │
          └───────────┬───────────┘  └──────────┬───────────┘
                      ↓                          ↓
          ┌───────────────────────┐  ┌──────────────────────┐
          │ transcription:results │  │ Disk Storage         │
          │ :{session_id}         │  │ data/chunks/*.wav    │
          └───────────┬───────────┘  └──────────────────────┘
                      ↓
          ┌───────────────────────┐
          │ TranscriptionResults  │
          │ Aggregator            │
          │ - Combines chunks     │
          │ - Merges timestamps   │
          └───────────┬───────────┘
                      ↓
          ┌───────────────────────┐
          │   RQ Job Pipeline     │
          ├───────────────────────┤
          │ speech_detection_job  │ ← Session-level
          │         ↓             │
          │ open_conversation_job │ ← Conversation-level
          │         ↓             │
          │ Post-Conversation:    │
          │ • transcribe_full     │
          │ • speaker_recognition │
          │ • memory_extraction   │
          │ • title_generation    │
          └───────────┬───────────┘
                      ↓
          ┌───────────────────────┐
          │   Final Storage       │
          ├───────────────────────┤
          │ MongoDB: conversations│
          │ Disk: WAV files       │
          │ Qdrant: Memories      │
          └───────────────────────┘
```

## Data Sources

### 1. WebSocket Streaming (`/ws`)

**Endpoint**: `/ws?codec=pcm|opus&token=xxx&device_name=xxx`

**Handlers**:
- `handle_pcm_websocket()` - Raw PCM audio
- `handle_omi_websocket()` - Opus-encoded audio (compressed, used by OMI devices)

**Protocol**: Wyoming Protocol (JSON lines + binary frames)

**Authentication**: JWT token required

**Location**: `backends/advanced/src/advanced_omi_backend/routers/websocket_routes.py`

**Container**: `chronicle-backend`

### 2. File Upload (`/audio/upload`)

**Endpoint**: `POST /api/audio/upload`

**Accepts**: Multiple WAV files (multipart form data)

**Authentication**: Admin only

**Device ID**: Auto-generated as `{user_id_suffix}-upload` or custom `device_name`

**Location**: `backends/advanced/src/advanced_omi_backend/routers/api_router.py`

**Container**: `chronicle-backend`

### 3. Google Drive Upload

**Endpoint**: `POST /api/audio/upload_audio_from_gdrive`

**Source**: Google Drive folder ID

**Processing**: Downloads files and enqueues for processing

**Container**: `chronicle-backend`

## Redis Streams: The Central Pipeline

### Stream Naming Convention

```
audio:stream:{client_id}
```

**Examples**:
- `audio:stream:user01-phone`
- `audio:stream:user01-omi-device`
- `audio:stream:user01-upload`

**Characteristics**:
- **Client-specific isolation**: Each device has its own stream
- **Fan-out pattern**: Multiple consumer groups read the same stream
- **MAXLEN constraint**: Keeps last 25,000 entries (auto-trimming)
- **No TTL**: Streams persist until manually deleted
- **Container**: `redis` service

### Session Metadata Storage

```
audio:session:{session_id}
```

**Type**: Redis Hash

**Fields**:
- `user_id`: MongoDB ObjectId
- `client_id`: Device identifier
- `connection_id`: WebSocket connection ID
- `stream_name`: `audio:stream:{client_id}`
- `status`: `"active"` → `"finalizing"` → `"complete"`
- `chunks_published`: Integer count
- `speech_detection_job_id`: RQ job ID
- `audio_persistence_job_id`: RQ job ID
- `websocket_connected`: `true|false`
- `transcription_error`: Error message (if any)

**TTL**: 1 hour

**Container**: `redis`

### Transcription Results Stream

```
transcription:results:{session_id}
```

**Type**: Redis Stream

**Written by**: Transcription consumers (streaming or batch)

**Read by**: `TranscriptionResultsAggregator`

**Message Fields**:
- `text`: Transcribed text for this chunk
- `chunk_id`: Redis message ID from audio stream
- `provider`: `"deepgram"` or `"parakeet"`
- `confidence`: Float (0.0-1.0)
- `words`: JSON array of word-level timestamps
- `segments`: JSON array of speaker segments

**Lifecycle**: Deleted when conversation completes

**Container**: `redis`

### Conversation Tracking

```
conversation:current:{session_id}
```

**Type**: Redis String

**Value**: Current `conversation_id` (UUID)

**Purpose**: Signals audio persistence job to rotate WAV file

**TTL**: 24 hours

**Container**: `redis`

### Audio File Path Mapping

```
audio:file:{conversation_id}
```

**Type**: Redis String

**Value**: File path (e.g., `1704067200000_user01-phone_convid.wav`)

**Purpose**: Links conversation to its audio file on disk

**TTL**: 24 hours

**Container**: `redis`

## Producer: AudioStreamProducer

**File**: `backends/advanced/src/advanced_omi_backend/services/audio_stream/producer.py`

**Container**: `chronicle-backend` (in-memory, no persistence)

### Responsibilities

#### 1. Session Initialization

```python
async def init_session(
    session_id: str,
    user_id: str,
    client_id: str,
    provider: str,
    mode: str
) -> None
```

**Actions**:
- Creates `audio:session:{session_id}` hash in Redis
- Initializes in-memory buffer for chunking
- Stores session metadata (user, client, provider)

#### 2. Audio Chunking

```python
async def add_audio_chunk(
    session_id: str,
    audio_data: bytes
) -> list[str]
```

**Process**:
1. Buffers incoming audio (arbitrary size from WebSocket)
2. Creates **fixed-size chunks**: 0.25 seconds = 8,000 bytes
   - Assumes: 16kHz sample rate, 16-bit mono PCM
3. Prevents cutting audio mid-word (aligned chunks)
4. Publishes each chunk to `audio:stream:{client_id}` via `XADD`
5. Returns Redis message IDs for tracking

**In-Memory Storage**: Session buffers stored in `AudioStreamProducer._session_buffers` dict

#### 3. Session End Signal

```python
async def send_session_end_signal(session_id: str) -> None
```

**Actions**:
- Publishes special `{"type": "END"}` message to stream
- Signals all consumers to flush buffers and finalize
- Updates session status to `"finalizing"`

### Data Location

**Memory**: `chronicle-backend` container (in-memory buffers)

**Redis**: Published chunks in `audio:stream:{client_id}` (redis container)

## Dual-Consumer Architecture

Chronicle uses **Redis Consumer Groups** to enable multiple independent consumers to read the **same audio stream** without message duplication.

### Consumer Group 1: Transcription

Two implementations available:

#### A. Streaming Transcription Consumer

**File**: `backends/advanced/src/advanced_omi_backend/services/transcription/streaming_consumer.py`

**Class**: `StreamingTranscriptionConsumer`

**Consumer Group**: `streaming-transcription`

**Provider**: Deepgram (WebSocket-based)

**Process**:
1. Discovers `audio:stream:*` streams dynamically using `SCAN`
2. Opens persistent WebSocket connection to Deepgram per stream
3. Sends audio chunks **immediately** (no buffering)
4. Publishes **interim results** to `transcription:interim:{session_id}` (Redis Pub/Sub)
5. Publishes **final results** to `transcription:results:{session_id}` (Redis Stream)
6. Triggers plugins on final results only
7. ACKs messages with `XACK` to prevent reprocessing
8. Handles END signal: closes WebSocket, cleans up

**Container**: `chronicle-backend` (Background Task via `BackgroundTaskManager`)

**Real-time Updates**: Interim results pushed to WebSocket clients via Pub/Sub

#### B. Batch Transcription Consumer

**File**: `backends/advanced/src/advanced_omi_backend/services/audio_stream/consumer.py`

**Class**: `BaseAudioStreamConsumer`

**Consumer Group**: `{provider_name}_workers` (e.g., `deepgram_workers`, `parakeet_workers`)

**Providers**: Deepgram (batch), Parakeet ASR (offline)

**Process**:
1. Reads from `audio:stream:{client_id}` using `XREADGROUP`
2. Buffers chunks per session (default: 30 chunks = ~7.5 seconds)
3. When buffer full:
   - Combines chunks into single audio buffer
   - Transcribes using provider API
   - Adjusts word/segment timestamps relative to session start
   - Publishes result to `transcription:results:{session_id}`
4. Flushes remaining buffer on END signal
5. ACKs all buffered messages with `XACK`
6. Trims stream to keep only last 1,000 entries (`XTRIM MAXLEN`)

**Container**: `chronicle-backend` (Background Task)

**Batching Benefits**: Reduces API calls, improves transcription accuracy (more context)

### Consumer Group 2: Audio Persistence

**File**: `backends/advanced/src/advanced_omi_backend/workers/audio_jobs.py`

**Function**: `audio_streaming_persistence_job()`

**Consumer Group**: `audio_persistence`

**Consumer Name**: `persistence-worker-{session_id}`

**Process**:
1. Reads audio chunks from `audio:stream:{client_id}` using `XREADGROUP`
2. Monitors `conversation:current:{session_id}` for rotation signals
3. On conversation rotation:
   - Closes current WAV file
   - Opens new WAV file with new conversation ID
4. Writes chunks immediately to disk (real-time persistence)
5. Stores file path in `audio:file:{conversation_id}` (Redis)
6. Handles END signal: closes file, returns statistics
7. ACKs messages after writing to disk

**Container**: `chronicle-backend` (RQ Worker)

**Output Location**: `backends/advanced/data/chunks/` (volume-mounted)

**File Format**: `{timestamp_ms}_{client_id}_{conversation_id}.wav`

### Fan-Out Pattern Visualization

```
audio:stream:user01-phone
    ↓
    ├─ Consumer Group: "streaming-transcription"
    │  └─ Worker: streaming-worker-12345
    │     → Reads: chunks → Deepgram WS → Results stream
    │
    ├─ Consumer Group: "deepgram_workers"
    │  ├─ Worker: deepgram-worker-67890
    │  ├─ Worker: deepgram-worker-67891
    │  └─ Reads: chunks → Buffer (30) → Batch API → Results stream
    │
    └─ Consumer Group: "audio_persistence"
       └─ Worker: persistence-worker-sessionXYZ
          → Reads: chunks → WAV file (disk)
```

**Key Benefits**:
- **Horizontal scaling**: Multiple workers per group
- **Independent processing**: Each group processes all messages
- **No message loss**: Messages ACKed only after processing
- **Decoupled**: Producer doesn't know about consumers

## Transcription Results Aggregator

**File**: `backends/advanced/src/advanced_omi_backend/services/audio_stream/aggregator.py`

**Class**: `TranscriptionResultsAggregator`

**Container**: `chronicle-backend` (in-memory, stateless)

### Methods

#### Get Combined Results

```python
async def get_combined_results(session_id: str) -> dict
```

**Returns**:
```python
{
    "text": "Full transcript...",
    "segments": [SpeakerSegment, ...],
    "words": [Word, ...],
    "provider": "deepgram",
    "chunk_count": 42
}
```

**Process**:
- Reads all entries from `transcription:results:{session_id}`
- For **streaming mode**: Uses latest final result only (supersedes interim)
- For **batch mode**: Combines all chunks sequentially
- Adjusts timestamps across chunks (adds audio offset)
- Merges speaker segments, words

#### Get Session Results (Raw)

```python
async def get_session_results(session_id: str) -> list[dict]
```

**Returns**: Raw list of transcription result messages

#### Get Real-time Results

```python
async def get_realtime_results(
    session_id: str,
    last_id: str = "0-0"
) -> tuple[list[dict], str]
```

**Returns**: `(new_results, new_last_id)`

**Purpose**: Incremental polling for live UI updates

### Data Location

**Input**: `transcription:results:{session_id}` stream (redis container)

**Processing**: In-memory (chronicle-backend container)

**Output**: Returned to caller (no persistence)

## Job Queue Orchestration (RQ)

**Library**: Python RQ (Redis Queue)

**File**: `backends/advanced/src/advanced_omi_backend/controllers/queue_controller.py`

**Containers**:
- `chronicle-backend` (enqueues jobs)
- `rq-worker` (executes jobs)

### Job Pipeline

```
Session Starts
    ↓
┌─────────────────────────────────┐
│ stream_speech_detection_job     │ ← Session-level (long-running)
│ - Polls transcription results   │
│ - Analyzes speech content       │
│ - Checks speaker filters        │
└─────────────┬───────────────────┘
              ↓ (when speech detected)
┌─────────────────────────────────┐
│ open_conversation_job           │ ← Conversation-level (long-running)
│ - Creates conversation          │
│ - Signals file rotation         │
│ - Monitors activity             │
│ - Detects end conditions        │
└─────────────┬───────────────────┘
              ↓ (when conversation ends)
┌─────────────────────────────────┐
│ Post-Conversation Pipeline      │
├─────────────────────────────────┤
│ • recognize_speakers_job        │
│ • memory_extraction_job         │
│ • generate_title_summary_job    │
│ • dispatch_conversation_complete│
└─────────────────────────────────┘
```

### Session-Level Jobs

#### Speech Detection Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/transcription_jobs.py`

**Function**: `stream_speech_detection_job()`

**Scope**: Entire session (can handle multiple conversations)

**Max Duration**: 24 hours

**Process**:
1. Polls `TranscriptionResultsAggregator.get_combined_results()` (1-second intervals)
2. Analyzes speech content:
   - Word count > 10
   - Duration > 5 seconds
   - Confidence > threshold
3. If speaker filter enabled: checks for enrolled speakers
4. When speech detected:
   - Creates conversation in MongoDB
   - Enqueues `open_conversation_job`
   - **Exits** (restarts when conversation completes)
5. Handles transcription errors (marks session with error flag)

**RQ Queue**: `speech_detection_queue` (dedicated queue)

**Container**: `rq-worker`

### Conversation-Level Jobs

#### Open Conversation Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/conversation_jobs.py`

**Function**: `open_conversation_job()`

**Scope**: Single conversation

**Max Duration**: 3 hours

**Process**:
1. Creates conversation document in MongoDB `conversations` collection
2. Sets `conversation:current:{session_id}` = `conversation_id` (Redis)
   - **Triggers audio persistence job to rotate WAV file**
3. Polls for transcription updates (1-second intervals)
4. Tracks speech activity (inactivity timeout = 60 seconds default)
5. Detects end conditions:
   - WebSocket disconnect
   - User manual stop
   - Inactivity timeout
6. Waits for audio file path from persistence job
7. Saves `audio_path` to conversation document
8. Triggers conversation-level plugins
9. Enqueues post-conversation jobs
10. Calls `handle_end_of_conversation()` for cleanup + restart

**RQ Queue**: `default`

**Container**: `rq-worker`

#### Audio Persistence Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/audio_jobs.py`

**Function**: `audio_streaming_persistence_job()`

**Scope**: Entire session (parallel with open_conversation_job)

**Max Duration**: 24 hours

**Process**:
1. Monitors `conversation:current:{session_id}` for rotation signals
2. For each conversation:
   - Opens new WAV file: `{timestamp}_{client_id}_{conversation_id}.wav`
   - Writes chunks immediately as they arrive from stream
   - Stores file path in `audio:file:{conversation_id}`
3. On rotation signal:
   - Closes current file
   - Opens new file for next conversation
4. On END signal:
   - Closes file
   - Returns statistics (chunk count, bytes, duration)

**Output**: WAV files in `backends/advanced/data/chunks/`

**Container**: `rq-worker`

### Post-Conversation Pipeline

**Streaming conversations**: Use streaming transcript saved during conversation. No batch re-transcription.

**File uploads**: Batch transcription job runs first, then post-conversation jobs depend on it.

#### 1. Recognize Speakers Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/transcription_jobs.py`

**Function**: `recognize_speakers_job()`

**Process**:
- Sends audio + segments to speaker recognition service
- Identifies speakers using voice embeddings
- Updates segment speaker labels in MongoDB

**Optional**: Only runs if `DISABLE_SPEAKER_RECOGNITION=false`

**Container**: `rq-worker`

**External Service**: `speaker-recognition` container (if enabled)

#### 2. Memory Extraction Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/memory_jobs.py`

**Function**: `memory_extraction_job()`

**Prerequisite**: Speaker recognition job

**Process**:
- Uses LLM (OpenAI/Ollama) to extract semantic facts
- Stores embeddings in vector database:
  - **Chronicle provider**: Qdrant
  - **OpenMemory MCP provider**: External OpenMemory server

**Container**: `rq-worker`

**External Services**:
- `ollama` or OpenAI API (LLM)
- `qdrant` or OpenMemory MCP (vector storage)

#### 3. Generate Title Summary Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/conversation_jobs.py`

**Function**: `generate_title_summary_job()`

**Prerequisite**: Speaker recognition job

**Process**:
- Uses LLM to generate title, summary, detailed summary
- Updates conversation document in MongoDB

**Container**: `rq-worker`

#### 4. Dispatch Conversation Complete Event

**File**: `backends/advanced/src/advanced_omi_backend/workers/conversation_jobs.py`

**Function**: `dispatch_conversation_complete_event_job()`

**Process**:
- Triggers `conversation.complete` plugin event

**Container**: `rq-worker`

#### Batch Transcription Job

**File**: `backends/advanced/src/advanced_omi_backend/workers/transcription_jobs.py`

**Function**: `transcribe_full_audio_job()`

**When used**:
- File uploads via `/api/process-audio-files`
- Manual reprocessing via `/api/conversations/{id}/reprocess-transcript`
- NOT used for streaming conversations

**Process**:
- Reconstructs audio from MongoDB chunks
- Batch transcribes entire audio
- Stores transcript with word-level timestamps

**Container**: `rq-worker`

### Session Restart

**File**: `backends/advanced/src/advanced_omi_backend/utils/conversation_utils.py`

**Function**: `handle_end_of_conversation()`

**Process**:
1. Deletes transcription results stream: `transcription:results:{session_id}`
2. Increments `session:conversation_count:{session_id}`
3. Checks if session still active (WebSocket connected)
4. If active: Re-enqueues `stream_speech_detection_job` for next conversation
5. Cleans up consumer groups and pending messages

**Purpose**: Allows continuous recording with multiple conversations per session

## Data Storage

### MongoDB Collections

**Database**: `chronicle`

**Container**: `mongo`

**Volume**: `mongodb_data` (persistent)

#### `conversations` Collection

**Schema**:
```python
{
    "_id": ObjectId,
    "conversation_id": "uuid-string",
    "audio_uuid": "session_id",
    "user_id": ObjectId,
    "client_id": "user01-phone",

    # Content
    "title": "Meeting notes",
    "summary": "Discussion about...",
    "detailed_summary": "Longer summary...",
    "transcript": "Full transcript text",
    "audio_path": "1704067200000_user01-phone_convid.wav",

    # Versioned Transcripts
    "active_transcript_version": "v1",
    "transcript_versions": {
        "v1": {
            "text": "Full transcript",
            "segments": [SpeakerSegment],
            "words": [Word],
            "provider": "deepgram",
            "processing_time_seconds": 45.2,
            "created_at": "2025-01-11T12:00:00Z"
        }
    },
    "segments": [SpeakerSegment],  # From active version

    # Metadata
    "created_at": "2025-01-11T12:00:00Z",
    "completed_at": "2025-01-11T12:15:00Z",
    "end_reason": "user_stopped|inactivity_timeout|websocket_disconnect",
    "deleted": false
}
```

**Indexes**:
- `user_id` (for user-scoped queries)
- `client_id` (for device filtering)
- `conversation_id` (unique)

#### `audio_chunks` Collection

**Purpose**: Stores raw audio session data

**Schema**:
```python
{
    "_id": ObjectId,
    "audio_uuid": "session_id",
    "user_id": ObjectId,
    "client_id": "user01-phone",
    "created_at": "2025-01-11T12:00:00Z",
    "metadata": { ... }
}
```

**Use Case**: Speech-driven architecture (sessions without conversations)

#### `users` Collection

**Purpose**: User accounts, authentication, preferences

**Schema**:
```python
{
    "_id": ObjectId,
    "email": "user@example.com",
    "hashed_password": "...",
    "is_active": true,
    "is_superuser": false,
    "created_at": "2025-01-11T12:00:00Z"
}
```

### Disk Storage

**Location**: `backends/advanced/data/chunks/`

**Container**: `chronicle-backend` (volume-mounted)

**Volume**: `./backends/advanced/data/chunks:/app/data/chunks`

**File Format**: WAV files

**Naming Convention**: `{timestamp_ms}_{client_id}_{conversation_id}.wav`

**Example**: `1704067200000_user01-phone_550e8400-e29b-41d4-a716-446655440000.wav`

**Created by**: `audio_streaming_persistence_job()`

**Read by**: Post-conversation transcription jobs

**Retention**: Manual cleanup (no automatic deletion)

### Redis Storage

**Container**: `redis`

**Volume**: `redis_data` (persistent)

| Key Pattern | Type | Purpose | TTL | Created By |
|-------------|------|---------|-----|------------|
| `audio:stream:{client_id}` | Stream | Audio chunks for transcription | None (MAXLEN=25k) | AudioStreamProducer |
| `audio:session:{session_id}` | Hash | Session metadata | 1 hour | AudioStreamProducer |
| `transcription:results:{session_id}` | Stream | Transcription results | Manual delete | Transcription consumers |
| `transcription:interim:{session_id}` | Pub/Sub | Real-time interim results | N/A (ephemeral) | Streaming consumer |
| `conversation:current:{session_id}` | String | Current conversation ID | 24 hours | open_conversation_job |
| `audio:file:{conversation_id}` | String | Audio file path | 24 hours | audio_persistence_job |
| `session:conversation_count:{session_id}` | Counter | Conversation count | 1 hour | handle_end_of_conversation |
| `speech_detection_job:{client_id}` | String | Job ID for cleanup | 1 hour | speech_detection_job |
| `rq:job:{job_id}` | Hash | RQ job metadata | 24 hours (default) | RQ |

### Vector Storage (Memory)

#### Option A: Qdrant (Chronicle Native Provider)

**Container**: `qdrant`

**Volume**: `qdrant_data` (persistent)

**Ports**: 6333 (HTTP), 6334 (gRPC)

**Collections**: User-specific collections for semantic embeddings

**Written by**: `memory_extraction_job()`

**Read by**: Memory search API (`/api/memories/search`)

#### Option B: OpenMemory MCP

**Container**: `openmemory-mcp` (external service)

**Port**: 8765

**Protocol**: MCP (Model Context Protocol)

**Collections**: Cross-client memory storage

**Written by**: `memory_extraction_job()` (via MCP provider)

**Read by**: Memory search API (via MCP provider)

## Complete End-to-End Flow

### Step-by-Step Data Journey

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. AUDIO INPUT                                                  │
└─────────────────────────────────────────────────────────────────┘
  WebSocket (/ws) or File Upload (/audio/upload)
  ↓
  Container: chronicle-backend
  ↓
  AudioStreamProducer.init_session()
  - Creates: audio:session:{session_id} (Redis)
  - Initializes: In-memory buffer (chronicle-backend container)
  ↓
  AudioStreamProducer.add_audio_chunk()
  - Buffers: In-memory (chronicle-backend)
  - Chunks: Fixed 0.25s chunks (8,000 bytes)
  - Publishes: audio:stream:{client_id} (Redis)
  - Returns: Redis message IDs

┌─────────────────────────────────────────────────────────────────┐
│ 2. SESSION-LEVEL JOB (RQ)                                       │
└─────────────────────────────────────────────────────────────────┘
  stream_speech_detection_job
  Container: rq-worker
  ↓
  Polls: TranscriptionResultsAggregator.get_combined_results()
  Reads: transcription:results:{session_id} (Redis)
  ↓
  Analyzes: Word count, duration, confidence
  ↓
  When speech detected:
    - Creates: Conversation document (MongoDB)
    - Enqueues: open_conversation_job (RQ)
    - Exits (restarts when conversation ends)

┌─────────────────────────────────────────────────────────────────┐
│ 3a. TRANSCRIPTION CONSUMER (Background Task)                    │
└─────────────────────────────────────────────────────────────────┘
  StreamingTranscriptionConsumer (or BaseAudioStreamConsumer)
  Container: chronicle-backend (Background Task)
  ↓
  Reads: audio:stream:{client_id} (Redis, via XREADGROUP)
  Consumer Group: streaming-transcription (or batch provider)
  ↓
  STREAMING PATH:
    • Opens: WebSocket to Deepgram
    • Sends: Chunks immediately (no buffering)
    • Publishes Interim: transcription:interim:{session_id} (Redis Pub/Sub)
    • Publishes Final: transcription:results:{session_id} (Redis Stream)
    • Triggers: Plugins on final results

  BATCH PATH:
    • Buffers: 30 chunks (~7.5s) in memory (chronicle-backend)
    • Combines: All buffered chunks
    • Transcribes: Via provider API (Deepgram/Parakeet)
    • Adjusts: Timestamps relative to session start
    • Publishes: transcription:results:{session_id} (Redis Stream)

┌─────────────────────────────────────────────────────────────────┐
│ 3b. AUDIO PERSISTENCE CONSUMER (RQ Job)                         │
└─────────────────────────────────────────────────────────────────┘
  audio_streaming_persistence_job
  Container: rq-worker
  ↓
  Reads: audio:stream:{client_id} (Redis, via XREADGROUP)
  Consumer Group: audio_persistence
  ↓
  Monitors: conversation:current:{session_id} (Redis)
  ↓
  For each conversation:
    • Opens: New WAV file (data/chunks/, chronicle-backend volume)
    • Writes: Chunks immediately (real-time)
    • Stores: audio:file:{conversation_id} = path (Redis)
  ↓
  On rotation signal:
    • Closes: Current file
    • Opens: New file for next conversation
  ↓
  On END signal:
    • Closes: File
    • Returns: Statistics (chunks, bytes, duration)

┌─────────────────────────────────────────────────────────────────┐
│ 4. CONVERSATION-LEVEL JOB (RQ)                                  │
└─────────────────────────────────────────────────────────────────┘
  open_conversation_job
  Container: rq-worker
  ↓
  Creates: Conversation document (MongoDB conversations collection)
  ↓
  Sets: conversation:current:{session_id} = conversation_id (Redis)
    → Triggers audio persistence job to rotate WAV file
  ↓
  Polls: TranscriptionResultsAggregator for updates (1s intervals)
  Reads: transcription:results:{session_id} (Redis)
  ↓
  Tracks: Speech activity (inactivity timeout = 60s)
  ↓
  Detects End:
    - Inactivity (60s)
    - User manual stop
    - WebSocket disconnect
  ↓
  Waits: For audio file path from persistence job
  Reads: audio:file:{conversation_id} (Redis)
  ↓
  Saves: audio_path to conversation document (MongoDB)
  ↓
  Enqueues: POST-CONVERSATION PIPELINE (RQ)

┌─────────────────────────────────────────────────────────────────┐
│ 5. POST-CONVERSATION PIPELINE (RQ - Parallel Jobs)              │
└─────────────────────────────────────────────────────────────────┘
  All jobs run in parallel
  Container: rq-worker
  ↓
  Reads: Audio file from disk (data/chunks/*.wav)

  ┌─ transcribe_full_audio_job
  │  - Batch transcribes: Complete audio file
  │  - Validates: Meaningful speech
  │  - Marks deleted: If no speech
  │  - Stores: MongoDB (transcript, segments, words)
  │
  │  └─ recognize_speakers_job (if enabled)
  │     - Sends: Audio + segments to speaker-recognition service
  │     - Identifies: Speakers via voice embeddings
  │     - Updates: MongoDB (segment speaker labels)
  │
  │  └─ memory_extraction_job
  │     - Uses: LLM (OpenAI/Ollama) to extract facts
  │     - Stores: Qdrant (Chronicle) or OpenMemory MCP (vector DB)
  │
  └─ generate_title_summary_job
     - Uses: LLM (OpenAI/Ollama)
     - Generates: Title, summary, detailed_summary
     - Stores: MongoDB (conversation document)

  └─ dispatch_conversation_complete_event_job
     - Triggers: conversation.complete plugins
     - Only for: File uploads (not streaming)

  All results stored: MongoDB conversations collection

┌─────────────────────────────────────────────────────────────────┐
│ 6. SESSION RESTART                                              │
└─────────────────────────────────────────────────────────────────┘
  handle_end_of_conversation()
  Container: chronicle-backend
  ↓
  Deletes: transcription:results:{session_id} (Redis)
  ↓
  Increments: session:conversation_count:{session_id} (Redis)
  ↓
  Checks: Session still active? (WebSocket connected)
  ↓
  If active:
    - Re-enqueues: stream_speech_detection_job (RQ)
    - Session remains: "active" for next conversation
```

### Data Locations Summary

| Stage | Data Type | Location | Container |
|-------|-----------|----------|-----------|
| Input | Audio bytes | In-memory buffers | chronicle-backend |
| Producer | Fixed chunks | `audio:stream:{client_id}` | redis |
| Session metadata | Hash | `audio:session:{session_id}` | redis |
| Transcription consumer | Interim results | `transcription:interim:{session_id}` (Pub/Sub) | redis |
| Transcription consumer | Final results | `transcription:results:{session_id}` (Stream) | redis |
| Audio persistence | WAV files | `data/chunks/*.wav` (disk volume) | chronicle-backend (volume) |
| Audio persistence | File paths | `audio:file:{conversation_id}` | redis |
| Conversation job | Conversation doc | MongoDB `conversations` | mongo |
| Post-processing | Transcript | MongoDB `conversations` | mongo |
| Post-processing | Memories | Qdrant or OpenMemory MCP | qdrant / openmemory-mcp |
| Post-processing | Title/summary | MongoDB `conversations` | mongo |

## Key Design Patterns

### 1. Speech-Driven Architecture

**Principle**: Conversations only created when speech is detected

**Benefits**:
- Clean user experience (no noise-only sessions in UI)
- Reduced memory processing load
- Automatic quality filtering

**Implementation**:
- `audio_chunks` collection: Always stores sessions
- `conversations` collection: Only created with speech
- Speech detection: Analyzes word count, duration, confidence

### 2. Versioned Processing

**Principle**: Store multiple versions of transcripts/memories

**Benefits**:
- Reprocess without losing originals
- A/B testing different providers
- Rollback to previous versions

**Implementation**:
- `transcript_versions` dict with version IDs (v1, v2, ...)
- `active_transcript_version` pointer
- `segments` field mirrors active version (quick access)

### 3. Session-Level vs Conversation-Level

**Session**: WebSocket connection lifetime (multiple conversations)
- Duration: Up to 24 hours
- Job: `stream_speech_detection_job`
- Purpose: Continuous monitoring for speech

**Conversation**: Speech burst between silence periods
- Duration: Typically minutes
- Job: `open_conversation_job`
- Purpose: Process single meaningful exchange

**Benefits**:
- Continuous recording without manual start/stop
- Automatic conversation segmentation
- Efficient resource usage (one session, many conversations)

### 4. Job Metadata Cascading

**Pattern**: Parent jobs link to child jobs

**Example**:
```
speech_detection_job
  ↓ job_id stored in
audio:session:{session_id}
  ↓ creates
open_conversation_job
  ↓ job_id stored in
conversation document
  ↓ creates
post-conversation jobs (parallel)
```

**Benefits**:
- Job grouping and cleanup
- Dependency tracking
- Debugging (trace job lineage)

### 5. Real-Time + Batch Hybrid

**Real-Time Path** (Streaming Consumer):
- Low latency (interim results in <1 second)
- WebSocket to Deepgram
- Publishes to Pub/Sub for live UI updates

**Batch Path** (Batch Consumer):
- High accuracy (more context)
- Buffers 7.5 seconds
- API-based transcription

**Both paths** write to same `transcription:results:{session_id}` stream

**Benefits**:
- Live UI updates (interim results)
- Accurate final results (batch processing)
- Provider flexibility (switch between streaming/batch)

### 6. Fan-Out via Redis Consumer Groups

**Pattern**: Multiple consumer groups read same stream

**Example**: `audio:stream:{client_id}` consumed by:
- Transcription consumer group
- Audio persistence consumer group

**Benefits**:
- Parallel processing paths
- Horizontal scaling (multiple workers per group)
- No message duplication (each group processes independently)

### 7. File Rotation via Redis Signals

**Pattern**: Conversation job signals persistence job via Redis key

**Implementation**:
```python
# Conversation job
redis.set(f"conversation:current:{session_id}", conversation_id)

# Persistence job (monitors key)
current_conv = redis.get(f"conversation:current:{session_id}")
if current_conv != last_conv:
    close_current_file()
    open_new_file(current_conv)
```

**Benefits**:
- Decoupled jobs (no direct communication)
- Real-time file rotation
- Multiple files per session (one per conversation)

## Failure Handling

### Transcription Errors

**Detection**: `stream_speech_detection_job` polls results

**Action**:
- Sets `transcription_error` field in `audio:session:{session_id}`
- Logs error for debugging
- Session remains active (can recover)

### No Meaningful Speech

**Detection**: `transcribe_full_audio_job` validates transcript

**Criteria**:
- Word count < 10
- Duration < 5 seconds
- All words low confidence

**Action**:
- Marks conversation `deleted=True`
- Sets `end_reason="no_meaningful_speech"`
- Conversation hidden from UI

### Audio File Not Ready

**Detection**: `open_conversation_job` waits for file path

**Timeout**: 30 seconds (configurable)

**Action**:
- Marks conversation `deleted=True`
- Sets `end_reason="audio_file_not_ready"`
- Logs error for debugging

### Job Zombies (Stuck Jobs)

**Detection**: `check_job_alive()` utility

**Method**: Checks Redis for job existence

**Action**:
- Returns `False` if job missing
- Caller can retry or fail gracefully

### Dead Consumers

**Detection**: Consumer group lag monitoring

**Cleanup**:
- Removes idle consumers (>30 seconds)
- Claims pending messages from dead consumers
- Redistributes to active workers

### Stream Trimming

**Prevention**: Streams don't grow unbounded

**Implementation**:
- `XTRIM MAXLEN 25000` on `audio:stream:{client_id}`
- Keeps last 25k messages (~104 minutes @ 0.25s chunks)
- Deletes `transcription:results:{session_id}` after conversation ends

### Session Timeout

**Max Duration**: 24 hours

**Action**:
- Jobs exit gracefully
- Session marked `"complete"`
- Resources cleaned up (streams deleted, consumer groups removed)

---

## Conclusion

Chronicle's audio pipeline is designed for:
- **Real-time processing**: Low-latency transcription and live UI updates
- **Horizontal scalability**: Redis Consumer Groups enable multiple workers
- **Fault tolerance**: Decoupled components, job retries, graceful error handling
- **Resource efficiency**: Speech-driven architecture filters noise automatically
- **Flexibility**: Pluggable providers (Deepgram/Parakeet, OpenAI/Ollama, Qdrant/OpenMemory)

All coordinated through **Redis Streams** for data flow and **RQ** for orchestration, with **MongoDB** for final storage and **disk** for audio archives.
