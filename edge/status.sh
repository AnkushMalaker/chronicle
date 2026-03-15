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

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <service-name> to see container status"
    echo "Available services: speaker-recognition asr-services tts llm-services havpe-relay"
    exit 0
fi

SERVICE_NAME="$1"

COMPOSE_PATH=$(cd "$CHRONICLE_HOME" && uv run --with-requirements setup-requirements.txt python3 -c "
import yaml, sys
with open('edge/services.yml') as f:
    data = yaml.safe_load(f)
svc = data.get('services', {}).get('$SERVICE_NAME')
if not svc:
    print('NOT_FOUND', file=sys.stderr); sys.exit(1)
print(svc['compose_path'])
" 2>/dev/null || echo "")

SERVICE_DIR="$CHRONICLE_HOME/$COMPOSE_PATH"

if [[ -z "$COMPOSE_PATH" || ! -d "$SERVICE_DIR" ]]; then
    echo "Service not found or not installed: $SERVICE_NAME"
    exit 1
fi

cd "$SERVICE_DIR"
echo "=== $SERVICE_NAME containers ==="
docker compose --profile edge ps
