#!/bin/bash
source "$(dirname "$0")/scripts/check_uv.sh"

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
