#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -n "${COMPOSE_CMD:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE_CMD}"
elif [[ "${CONTAINER_ENGINE:-docker}" == "podman" ]]; then
  compose_command=(podman-compose)
else
  compose_command=(docker compose)
fi

exec "${compose_command[@]}" run --rm --no-deps chronicle-backend \
  uv run --offline --no-sync python3 src/scripts/chronicle_data.py "$@"
