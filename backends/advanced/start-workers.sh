#!/bin/bash
# Unified worker startup script
# Starts all workers in a single container for efficiency

set -e

echo "🚀 Starting Chronicle Workers..."

# Clean up any stale worker registrations from previous runs
echo "🧹 Cleaning up stale worker registrations from Redis..."
# Use RQ's cleanup command to remove dead workers
uv run python -c "
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
    if worker.hostname == hostname:
        worker.register_death()
        cleaned += 1
print(f'Cleaned up {cleaned} stale workers from {hostname}')
" 2>/dev/null || echo "No stale workers to clean"

sleep 1

# Function to start all workers
start_workers() {
    echo "🔧 Starting RQ workers (6 workers, all queues: transcription, memory, default)..."
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_1_PID=$!
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_2_PID=$!
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_3_PID=$!
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_4_PID=$!
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_5_PID=$!
    uv run python -m advanced_omi_backend.workers.rq_worker_entry transcription memory default &
    RQ_WORKER_6_PID=$!

    echo "💾 Starting audio persistence worker (1 worker for audio queue)..."
    uv run python -m advanced_omi_backend.workers.rq_worker_entry audio &
    AUDIO_PERSISTENCE_WORKER_PID=$!

    # Determine which STT provider to use from config.yml
    echo "📋 Checking config.yml for default STT provider..."
    DEFAULT_STT=$(uv run python -c "
from advanced_omi_backend.model_registry import get_models_registry
registry = get_models_registry()
if registry and registry.defaults:
    stt_model = registry.get_default('stt')
    if stt_model:
        print(stt_model.model_provider or '')
" 2>/dev/null || echo "")

    echo "📋 Configured STT provider: ${DEFAULT_STT:-none}"

    # Only start Deepgram worker if configured as default STT
    if [[ "$DEFAULT_STT" == "deepgram" ]] && [ -n "$DEEPGRAM_API_KEY" ]; then
        echo "🎵 Starting audio stream Deepgram worker (1 worker for sequential processing)..."
        uv run python -m advanced_omi_backend.workers.audio_stream_deepgram_worker &
        AUDIO_STREAM_DEEPGRAM_WORKER_PID=$!
    else
        echo "⏭️  Skipping Deepgram stream worker (not configured as default STT or API key missing)"
        AUDIO_STREAM_DEEPGRAM_WORKER_PID=""
    fi

    # Only start Parakeet worker if configured as default STT
    if [[ "$DEFAULT_STT" == "parakeet" ]]; then
        echo "🎵 Starting audio stream Parakeet worker (1 worker for sequential processing)..."
        uv run python -m advanced_omi_backend.workers.audio_stream_parakeet_worker &
        AUDIO_STREAM_PARAKEET_WORKER_PID=$!
    else
        echo "⏭️  Skipping Parakeet stream worker (not configured as default STT)"
        AUDIO_STREAM_PARAKEET_WORKER_PID=""
    fi

    echo "✅ All workers started:"
    echo "  - RQ worker 1: PID $RQ_WORKER_1_PID (transcription, memory, default)"
    echo "  - RQ worker 2: PID $RQ_WORKER_2_PID (transcription, memory, default)"
    echo "  - RQ worker 3: PID $RQ_WORKER_3_PID (transcription, memory, default)"
    echo "  - RQ worker 4: PID $RQ_WORKER_4_PID (transcription, memory, default)"
    echo "  - RQ worker 5: PID $RQ_WORKER_5_PID (transcription, memory, default)"
    echo "  - RQ worker 6: PID $RQ_WORKER_6_PID (transcription, memory, default)"
    echo "  - Audio persistence worker: PID $AUDIO_PERSISTENCE_WORKER_PID (audio queue - file rotation)"
    [ -n "$AUDIO_STREAM_DEEPGRAM_WORKER_PID" ] && echo "  - Audio stream Deepgram worker: PID $AUDIO_STREAM_DEEPGRAM_WORKER_PID (Redis Streams consumer)" || true
    [ -n "$AUDIO_STREAM_PARAKEET_WORKER_PID" ] && echo "  - Audio stream Parakeet worker: PID $AUDIO_STREAM_PARAKEET_WORKER_PID (Redis Streams consumer)" || true
}

# Function to check worker registration health
check_worker_health() {
    WORKER_COUNT=$(uv run python -c "
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
    local MIN_WORKERS=6      # Expect at least 6 RQ workers

    echo "🩺 Starting self-healing monitor (check interval: ${CHECK_INTERVAL}s, min workers: ${MIN_WORKERS})"

    while true; do
        sleep $CHECK_INTERVAL

        WORKER_COUNT=$(check_worker_health)

        if [ "$WORKER_COUNT" -lt "$MIN_WORKERS" ]; then
            echo "⚠️ Self-healing: Only $WORKER_COUNT workers registered (expected >= $MIN_WORKERS)"
            echo "🔧 Self-healing: Restarting all workers to restore registration..."

            # Kill all workers
            kill $RQ_WORKER_1_PID $RQ_WORKER_2_PID $RQ_WORKER_3_PID $RQ_WORKER_4_PID $RQ_WORKER_5_PID $RQ_WORKER_6_PID $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
            [ -n "$AUDIO_STREAM_DEEPGRAM_WORKER_PID" ] && kill $AUDIO_STREAM_DEEPGRAM_WORKER_PID 2>/dev/null || true
            [ -n "$AUDIO_STREAM_PARAKEET_WORKER_PID" ] && kill $AUDIO_STREAM_PARAKEET_WORKER_PID 2>/dev/null || true
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

# Function to handle shutdown
shutdown() {
    echo "🛑 Shutting down workers..."
    kill $MONITOR_PID 2>/dev/null || true
    kill $RQ_WORKER_1_PID 2>/dev/null || true
    kill $RQ_WORKER_2_PID 2>/dev/null || true
    kill $RQ_WORKER_3_PID 2>/dev/null || true
    kill $RQ_WORKER_4_PID 2>/dev/null || true
    kill $RQ_WORKER_5_PID 2>/dev/null || true
    kill $RQ_WORKER_6_PID 2>/dev/null || true
    kill $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
    [ -n "$AUDIO_STREAM_DEEPGRAM_WORKER_PID" ] && kill $AUDIO_STREAM_DEEPGRAM_WORKER_PID 2>/dev/null || true
    [ -n "$AUDIO_STREAM_PARAKEET_WORKER_PID" ] && kill $AUDIO_STREAM_PARAKEET_WORKER_PID 2>/dev/null || true
    wait
    echo "✅ All workers stopped"
    exit 0
}

# Set up signal handlers
trap shutdown SIGTERM SIGINT

# Configure Python logging for RQ workers
export PYTHONUNBUFFERED=1

# Start all workers
start_workers

# Start self-healing monitor in background
monitor_worker_health &
MONITOR_PID=$!
echo "🩺 Self-healing monitor started: PID $MONITOR_PID"

# Keep the script running and let the self-healing monitor handle worker failures
# Don't use wait -n (fail-fast on first worker exit) - this kills all workers when one fails
# Instead, wait for the monitor process or explicit shutdown signal
echo "⏳ Workers running - self-healing monitor will restart failed workers automatically"
wait $MONITOR_PID

# If monitor exits (should only happen on SIGTERM/SIGINT), shut down gracefully
echo "🛑 Monitor exited, shutting down all workers..."
kill $RQ_WORKER_1_PID 2>/dev/null || true
kill $RQ_WORKER_2_PID 2>/dev/null || true
kill $RQ_WORKER_3_PID 2>/dev/null || true
kill $RQ_WORKER_4_PID 2>/dev/null || true
kill $RQ_WORKER_5_PID 2>/dev/null || true
kill $RQ_WORKER_6_PID 2>/dev/null || true
kill $AUDIO_PERSISTENCE_WORKER_PID 2>/dev/null || true
[ -n "$AUDIO_STREAM_DEEPGRAM_WORKER_PID" ] && kill $AUDIO_STREAM_DEEPGRAM_WORKER_PID 2>/dev/null || true
[ -n "$AUDIO_STREAM_PARAKEET_WORKER_PID" ] && kill $AUDIO_STREAM_PARAKEET_WORKER_PID 2>/dev/null || true
wait

echo "✅ All workers stopped gracefully"
exit 0
