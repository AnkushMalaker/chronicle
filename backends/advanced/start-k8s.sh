#!/bin/bash

# Chronicle Backend Kubernetes Startup Script
# Starts both the FastAPI backend and RQ workers for K8s deployment

set -e

echo "🚀 Starting Chronicle Backend (Kubernetes)..."

# Debug environment variables
echo "🔍 Environment check:"
echo "  REDIS_URL: ${REDIS_URL:-NOT_SET}"
echo "  MONGODB_URI: ${MONGODB_URI:-NOT_SET}"

# Function to handle shutdown
shutdown() {
    echo "🛑 Shutting down services..."
    kill $MONITOR_PID 2>/dev/null || true
    kill $AUDIO_WORKER_1_PID 2>/dev/null || true
    kill $RQ_WORKER_1_PID 2>/dev/null || true
    kill $RQ_WORKER_2_PID 2>/dev/null || true
    kill $RQ_WORKER_3_PID 2>/dev/null || true
    kill $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
    kill $BACKEND_PID 2>/dev/null || true
    wait
    echo "✅ All services stopped"
    exit 0
}

# Set up signal handlers
trap shutdown SIGTERM SIGINT

# Test Redis connectivity first
echo "🔍 Testing Redis connectivity..."
if [ -n "${REDIS_URL}" ]; then
    echo "  Using Redis URL: ${REDIS_URL}"
    # Try to ping Redis to verify connectivity
    timeout 5 python3 -c "
import redis
import sys
import os
try:
    r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
    r.ping()
    print('  ✅ Redis connection successful')
except Exception as e:
    print(f'  ❌ Redis connection failed: {e}')
    sys.exit(1)
" || {
        echo "  ❌ Redis connectivity test failed, continuing anyway..."
    }
else
    echo "  ⚠️  REDIS_URL not set, using default"
fi

# Clean up stale worker registrations from previous runs
echo "🧹 Cleaning up stale worker registrations from Redis..."
python3 -c "
from rq import Worker
from redis import Redis
import os
import socket

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_conn = Redis.from_url(redis_url)
hostname = socket.gethostname()

# Only clean up workers from THIS hostname (pod)
workers = Worker.all(connection=redis_conn)
cleaned = 0
for worker in workers:
    if hostname in worker.name:
        worker.register_death()
        cleaned += 1
print(f'Cleaned up {cleaned} stale workers from {hostname}')
" 2>/dev/null || echo "No stale workers to clean"

sleep 1

# Function to start all workers
start_workers() {
    # NEW WORKERS - Registry-driven streaming transcription architecture
    # Single worker ensures sequential processing of audio chunks (matching worker_orchestrator.py)
    # Uses config.yml for provider selection (Deepgram, Parakeet, etc.)
    echo "🎵 Starting streaming transcription worker (registry-driven provider from config.yml)..."
    if python3 -m advanced_omi_backend.workers.audio_stream_worker &
    then
        AUDIO_WORKER_1_PID=$!
        echo "  ✅ Streaming transcription worker started with PID: $AUDIO_WORKER_1_PID"
    else
        echo "  ❌ Failed to start streaming transcription worker"
        exit 1
    fi

    # Start 3 RQ workers listening to ALL queues (matching worker_orchestrator.py)
    echo "🔧 Starting RQ workers (3 workers, all queues: transcription, memory, default)..."
    if python3 -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    then
        RQ_WORKER_1_PID=$!
        echo "  ✅ RQ worker 1 started with PID: $RQ_WORKER_1_PID"
    else
        echo "  ❌ Failed to start RQ worker 1"
        kill $AUDIO_WORKER_1_PID 2>/dev/null || true
        exit 1
    fi

    if python3 -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    then
        RQ_WORKER_2_PID=$!
        echo "  ✅ RQ worker 2 started with PID: $RQ_WORKER_2_PID"
    else
        echo "  ❌ Failed to start RQ worker 2"
        kill $AUDIO_WORKER_1_PID $RQ_WORKER_1_PID 2>/dev/null || true
        exit 1
    fi

    if python3 -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    then
        RQ_WORKER_3_PID=$!
        echo "  ✅ RQ worker 3 started with PID: $RQ_WORKER_3_PID"
    else
        echo "  ❌ Failed to start RQ worker 3"
        kill $AUDIO_WORKER_1_PID $RQ_WORKER_1_PID $RQ_WORKER_2_PID 2>/dev/null || true
        exit 1
    fi

    # Start 1 dedicated audio persistence worker (matching worker_orchestrator.py)
    echo "💾 Starting audio persistence worker (1 worker for audio queue)..."
    if python3 -m advanced_omi_backend.workers.rq_worker_entry audio &
    then
        AUDIO_PERSISTENCE_WORKER_PID=$!
        echo "  ✅ Audio persistence worker started with PID: $AUDIO_PERSISTENCE_WORKER_PID"
    else
        echo "  ❌ Failed to start audio persistence worker"
        kill $AUDIO_WORKER_1_PID $RQ_WORKER_1_PID $RQ_WORKER_2_PID $RQ_WORKER_3_PID 2>/dev/null || true
        exit 1
    fi

    echo "✅ All workers started:"
    echo "  - Audio stream worker: $AUDIO_WORKER_1_PID (Redis Streams consumer - sequential processing)"
    echo "  - RQ worker 1: $RQ_WORKER_1_PID (transcription, memory, default)"
    echo "  - RQ worker 2: $RQ_WORKER_2_PID (transcription, memory, default)"
    echo "  - RQ worker 3: $RQ_WORKER_3_PID (transcription, memory, default)"
    echo "  - Audio persistence worker: $AUDIO_PERSISTENCE_WORKER_PID (audio queue - file rotation)"
}

# Function to check worker registration health
check_worker_health() {
    WORKER_COUNT=$(python3 -c "
from rq import Worker
from redis import Redis
import os
import sys

try:
    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    r = Redis.from_url(redis_url)
    workers = Worker.all(connection=r)
    print(len(workers))
except Exception as e:
    print('0', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null || echo "0")
    echo "$WORKER_COUNT"
}

# Self-healing monitoring function
monitor_worker_health() {
    local CHECK_INTERVAL=10  # Check every 10 seconds
    local MIN_WORKERS=3      # Expect at least 3 RQ workers

    echo "🩺 Starting self-healing monitor (check interval: ${CHECK_INTERVAL}s, min workers: ${MIN_WORKERS})"

    while true; do
        sleep $CHECK_INTERVAL

        WORKER_COUNT=$(check_worker_health)

        if [ "$WORKER_COUNT" -lt "$MIN_WORKERS" ]; then
            echo "⚠️ Self-healing: Only $WORKER_COUNT workers registered (expected >= $MIN_WORKERS)"
            echo "🔧 Self-healing: Restarting all workers to restore registration..."

            # Kill all workers
            kill $AUDIO_WORKER_1_PID $RQ_WORKER_1_PID $RQ_WORKER_2_PID $RQ_WORKER_3_PID $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
            wait 2>/dev/null || true

            # Restart workers
            start_workers

            # Verify recovery
            sleep 3
            NEW_WORKER_COUNT=$(check_worker_health)
            echo "✅ Self-healing: Workers restarted - new count: $NEW_WORKER_COUNT"
        fi
    done
}

# OLD WORKERS - Disabled for testing new Redis Streams architecture
# These have been renamed to old_audio_stream_worker.py and old_transcription_stream_worker.py
# echo "🎵 Starting Redis Streams audio workers (2 workers)..."
# if uv run --no-sync python3 -m advanced_omi_backend.workers.old_audio_stream_worker &
# then
#     AUDIO_WORKER_1_PID=$!
#     echo "  ✅ Audio stream worker 1 started with PID: $AUDIO_WORKER_1_PID"
# else
#     echo "  ❌ Failed to start audio stream worker 1"
#     exit 1
# fi

# if uv run --no-sync python3 -m advanced_omi_backend.workers.old_audio_stream_worker &
# then
#     AUDIO_WORKER_2_PID=$!
#     echo "  ✅ Audio stream worker 2 started with PID: $AUDIO_WORKER_2_PID"
# else
#     echo "  ❌ Failed to start audio stream worker 2"
#     kill $AUDIO_WORKER_1_PID 2>/dev/null || true
#     exit 1
# fi

# echo "📝 Starting transcription stream workers (2 workers)..."
# if uv run --no-sync python3 -m advanced_omi_backend.workers.old_transcription_stream_worker &
# then
#     TRANSCRIPTION_WORKER_1_PID=$!
#     echo "  ✅ Transcription stream worker 1 started with PID: $TRANSCRIPTION_WORKER_1_PID"
# else
#     echo "  ❌ Failed to start transcription stream worker 1"
#     kill $AUDIO_WORKER_1_PID $AUDIO_WORKER_2_PID 2>/dev/null || true
#     exit 1
# fi

# if uv run --no-sync python3 -m advanced_omi_backend.workers.old_transcription_stream_worker &
# then
#     TRANSCRIPTION_WORKER_2_PID=$!
#     echo "  ✅ Transcription stream worker 2 started with PID: $TRANSCRIPTION_WORKER_2_PID"
# else
#     echo "  ❌ Failed to start transcription stream worker 2"
#     kill $AUDIO_WORKER_1_PID $AUDIO_WORKER_2_PID $TRANSCRIPTION_WORKER_1_PID 2>/dev/null || true
#     exit 1
# fi

# Configure Python logging for workers
export PYTHONUNBUFFERED=1

# Start all workers
start_workers

# Start self-healing monitor in background
monitor_worker_health &
MONITOR_PID=$!
echo "🩺 Self-healing monitor started: PID $MONITOR_PID"

# Give workers a moment to start
sleep 3

# Start the main FastAPI application
echo "🌐 Starting FastAPI backend..."
if python3 src/advanced_omi_backend/main.py &
then
    BACKEND_PID=$!
    echo "  ✅ FastAPI backend started with PID: $BACKEND_PID"
else
    echo "  ❌ Failed to start FastAPI backend"
    kill $AUDIO_WORKER_1_PID $RQ_WORKER_1_PID $RQ_WORKER_2_PID $RQ_WORKER_3_PID $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
    exit 1
fi

echo "🎉 All services started successfully!"
echo "  - Audio stream worker: $AUDIO_WORKER_1_PID (Redis Streams consumer - sequential processing)"
echo "  - RQ worker 1: $RQ_WORKER_1_PID (transcription, memory, default)"
echo "  - RQ worker 2: $RQ_WORKER_2_PID (transcription, memory, default)"
echo "  - RQ worker 3: $RQ_WORKER_3_PID (transcription, memory, default)"
echo "  - Audio persistence worker: $AUDIO_PERSISTENCE_WORKER_PID (audio queue - file rotation)"
echo "  - Self-healing monitor: $MONITOR_PID"
echo "  - FastAPI Backend: $BACKEND_PID"

# Wait for any process to exit
wait -n

# If we get here, one process has exited - kill the others
echo "⚠️  One service exited, stopping all services..."
# Kill only non-empty PIDs
[ -n "$MONITOR_PID" ] && kill $MONITOR_PID 2>/dev/null || true
[ -n "$AUDIO_WORKER_1_PID" ] && kill $AUDIO_WORKER_1_PID 2>/dev/null || true
[ -n "$RQ_WORKER_1_PID" ] && kill $RQ_WORKER_1_PID 2>/dev/null || true
[ -n "$RQ_WORKER_2_PID" ] && kill $RQ_WORKER_2_PID 2>/dev/null || true
[ -n "$RQ_WORKER_3_PID" ] && kill $RQ_WORKER_3_PID 2>/dev/null || true
[ -n "$AUDIO_PERSISTENCE_WORKER_PID" ] && kill $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
[ -n "$BACKEND_PID" ] && kill $BACKEND_PID 2>/dev/null || true
wait

echo "🔄 All services stopped"
exit 1
