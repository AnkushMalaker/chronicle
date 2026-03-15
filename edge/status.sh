#!/usr/bin/env bash
# Show status of an edge-deployed Chronicle service.
#
# Usage: ./status.sh [service-name]
#   If no service name given, shows Tailscale IP only.
set -euo pipefail

CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/chronicle}"

echo "=== Chronicle Edge Status ==="
echo ""

# Tailscale info
if command -v tailscale &>/dev/null; then
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "not connected")
    echo "Tailscale IP: $TAILSCALE_IP"
else
    echo "Tailscale: not installed"
fi
echo ""

declare -A SERVICE_PATHS=(
    [speaker-recognition]=extras/speaker-recognition
    [asr-services]=extras/asr-services
    [tts]=extras/tts
    [llm-services]=extras/llm-services
    [havpe-relay]=extras/havpe-relay
)

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <service-name> to see container status"
    echo "Available services: ${!SERVICE_PATHS[*]}"
    exit 0
fi

SERVICE_NAME="$1"

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
echo "=== $SERVICE_NAME containers ==="
docker compose --profile edge ps
