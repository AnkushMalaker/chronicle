# shellcheck shell=bash
# Resolve the container engine + compose command for the test harness so it runs
# under Podman as well as Docker. Source this from the bin/ scripts:
#
#     source "$(dirname "$0")/_engine.sh"
#     $COMPOSE -f docker-compose-test.yml up -d      # docker compose | podman-compose
#     $ENGINE inspect ...                            # docker | podman
#
# Precedence: CONTAINER_ENGINE / COMPOSE_CMD env  →  config/config.yml
# container_engine  →  docker default. Mirrors services.py's engine selection.

_engine_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_engine_config="$_engine_repo_root/config/config.yml"

if [ -z "${CONTAINER_ENGINE:-}" ] && [ -f "$_engine_config" ]; then
    CONTAINER_ENGINE="$(grep -E '^container_engine:' "$_engine_config" \
        | head -1 | awk '{print $2}' | tr -d "\"'")"
fi

ENGINE="${CONTAINER_ENGINE:-docker}"

if [ -n "${COMPOSE_CMD:-}" ]; then
    COMPOSE="$COMPOSE_CMD"
elif [ "$ENGINE" = "podman" ]; then
    COMPOSE="podman-compose"
else
    COMPOSE="docker compose"
fi

export ENGINE COMPOSE
