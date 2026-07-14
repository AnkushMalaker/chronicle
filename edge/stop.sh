#!/usr/bin/env bash
# Stop an edge-deployed Chronicle service.
#
# Usage: ./stop.sh <service-name>
set -euo pipefail

CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/chronicle}"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <service-name>"
    echo "Example: $0 speaker-recognition"
    exit 1
fi

SERVICE_NAME="$1"

declare -A SERVICE_PATHS=(
    [speaker-recognition]=extras/speaker-recognition
    [asr-services]=extras/asr-services
    [tts]=extras/tts
    [llm-services]=extras/llm-services
    [havpe-relay]=extras/havpe-relay
)

if [[ -z "${SERVICE_PATHS[$SERVICE_NAME]+_}" ]]; then
    echo "Unknown service: $SERVICE_NAME"
    echo "Available: ${!SERVICE_PATHS[*]}"
    exit 1
fi

SERVICE_DIR="$CHRONICLE_HOME/${SERVICE_PATHS[$SERVICE_NAME]}"

if [[ ! -d "$SERVICE_DIR" ]]; then
    echo "Service not found or not installed: $SERVICE_NAME"
    exit 1
fi

cd "$SERVICE_DIR"
docker compose --profile edge down
echo "$SERVICE_NAME stopped."
