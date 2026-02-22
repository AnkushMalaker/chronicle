#!/bin/bash
source "$(dirname "$0")/scripts/check_uv.sh"
uv run --with-requirements setup-requirements.txt python services.py stop --all
