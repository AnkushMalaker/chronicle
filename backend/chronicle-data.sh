#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Engine selection follows services.py: env first, then config.yml, then a default.
# Reading config.yml matters -- a podman host sets `container_engine: podman` there and
# nowhere else, so defaulting straight to docker made this script the one part of the
# stack that could not run on it ("exec: docker: not found").
engine="${CONTAINER_ENGINE:-}"
if [[ -z "${engine}" ]]; then
  engine=$(sed -n 's/^container_engine:[[:space:]]*\([a-z-]*\).*/\1/p' \
    ../config/config.yml 2>/dev/null | head -1)
fi

if [[ -n "${COMPOSE_CMD:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE_CMD}"
elif [[ "${engine}" == "podman" ]]; then
  compose_command=(podman-compose)
else
  compose_command=(docker compose)
fi

# Every subcommand here is a bulk scan, not request/response traffic: an export streams
# every audio chunk through one cursor. The shared client's 20s socketTimeoutMS is right
# for the API and fatal here -- one slow getMore aborts the whole backup -- and PyMongo
# gives explicit kwargs precedence over URI options, so this is the only way to raise it.
exec "${compose_command[@]}" run --rm --no-deps \
  -e MONGODB_SOCKET_TIMEOUT_MS="${MONGODB_SOCKET_TIMEOUT_MS:-1800000}" \
  chronicle-backend \
  uv run --offline --no-sync python3 src/scripts/chronicle_data.py "$@"
