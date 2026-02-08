#!/bin/bash

# Chronicle Backend Startup Script
# Starts both the FastAPI backend and RQ workers
# Usage: ./start.sh [--test]

set -e

# Check for test mode flag
TEST_MODE=false
if [[ "$1" == "--test" ]]; then
    TEST_MODE=true
    echo "🧪 Running in TEST mode (with test dependencies)"
fi

echo "🚀 Starting Chronicle Backend..."

# Function to handle shutdown
shutdown() {
    echo "🛑 Shutting down services..."
    # Kill the backend process if running
    [ -n "$BACKEND_PID" ] && kill -TERM $BACKEND_PID 2>/dev/null || true
    wait
    echo "✅ All services stopped"
    exit 0
}

# Set up signal handlers
trap shutdown SIGTERM SIGINT

# OLD WORKERS - Disabled for testing new Redis Streams architecture
# These have been renamed to old_audio_stream_worker.py and old_transcription_stream_worker.py
# echo "🎵 Starting Redis Streams audio workers (2 workers)..."
# uv run --extra deepgram python3 -m advanced_omi_backend.workers.old_audio_stream_worker &
# AUDIO_WORKER_1_PID=$!
# uv run --extra deepgram python3 -m advanced_omi_backend.workers.old_audio_stream_worker &
# AUDIO_WORKER_2_PID=$!

# echo "📝 Starting transcription stream workers (2 workers)..."
# uv run --extra deepgram python3 -m advanced_omi_backend.workers.old_transcription_stream_worker &
# TRANSCRIPTION_WORKER_1_PID=$!
# uv run --extra deepgram python3 -m advanced_omi_backend.workers.old_transcription_stream_worker &
# TRANSCRIPTION_WORKER_2_PID=$!

# NEW WORKERS - Redis Streams multi-provider architecture
# Note: Workers are now started via docker-compose as dedicated services
# See: audio-stream-worker-1 and audio-stream-worker-2 in docker-compose.yml
echo "ℹ️  Audio stream workers run as dedicated docker-compose services"
AUDIO_WORKER_1_PID=""
AUDIO_WORKER_2_PID=""
TRANSCRIPTION_WORKER_1_PID=""
TRANSCRIPTION_WORKER_2_PID=""

# RQ workers are now started via docker-compose as a dedicated service
# See: workers service in docker-compose.yml
echo "ℹ️  RQ workers run as dedicated docker-compose service"
RQ_WORKER_PID=""

# Give workers a moment to start
sleep 2

# Start the main FastAPI application
echo "🌐 Starting FastAPI backend..."
# Use --group test in test mode
if [ "$TEST_MODE" = true ]; then
    uv run --extra deepgram --group test python3 src/advanced_omi_backend/main.py &
else
    uv run --extra deepgram python3 src/advanced_omi_backend/main.py &
fi
BACKEND_PID=$!

# Wait for any process to exit
wait -n

# If we get here, one process has exited - kill the others
echo "⚠️  One service exited, stopping all services..."
# Kill only non-empty PIDs
[ -n "$AUDIO_WORKER_1_PID" ] && kill $AUDIO_WORKER_1_PID 2>/dev/null || true
[ -n "$AUDIO_WORKER_2_PID" ] && kill $AUDIO_WORKER_2_PID 2>/dev/null || true
[ -n "$RQ_WORKER_PID" ] && kill $RQ_WORKER_PID 2>/dev/null || true
[ -n "$BACKEND_PID" ] && kill $BACKEND_PID 2>/dev/null || true
wait

echo "🔄 All services stopped"
exit 1