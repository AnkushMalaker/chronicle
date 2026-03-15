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

# Parse compose_path from registry
COMPOSE_PATH=$(uv run python3 -c "
import yaml, sys
with open('$CHRONICLE_HOME/edge/services.yml') as f:
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
docker compose --profile edge down
echo "$SERVICE_NAME stopped."
