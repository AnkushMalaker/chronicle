#!/bin/bash
source "$(dirname "$0")/scripts/check_uv.sh"

# Run from the repository root: setup-requirements.txt lists chronicle-setup as a
# relative path, and uv resolves that against the working directory.
cd "$(dirname "$0")" || exit 1

# If the first argument is a known subcommand, pass it directly to services.py
# instead of prepending "start --all". This lets "./start.sh status" work correctly.
case "${1:-}" in
    status|stop|restart)
        uv run --with-requirements setup-requirements.txt python services.py "$@"
        ;;
    *)
        uv run --with-requirements setup-requirements.txt python services.py start --all "$@"
        ;;
esac
