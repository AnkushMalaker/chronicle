#!/bin/bash
# Stage a nanowakeword wheel into vendor/ for the Docker build. Source of truth
# is the FORK https://github.com/AnkushMalaker/nanowakeword (carries the
# determinism fixes: deduped optimizer params, NWW_SEED/NWW_DETERMINISTIC).
# PyPI tops out at 2.1.3 and upstream 2.1.4 lacks the fixes.
# Run this BEFORE `docker compose build`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../untracked/nanowakeword"
VENDOR="$HERE/vendor"
FORK_URL="https://github.com/AnkushMalaker/nanowakeword"

if [ ! -d "$SRC" ]; then
  echo "Cloning fork into $SRC ..."
  git clone "$FORK_URL" "$SRC"
fi
echo "Building from: $(git -C "$SRC" log -1 --oneline) ($(git -C "$SRC" remote get-url origin))"

mkdir -p "$VENDOR"
rm -f "$VENDOR"/nanowakeword-*.whl
echo "Building nanowakeword wheel from $SRC ..."
uv build --wheel --project "$SRC" --out-dir "$VENDOR"
echo "Staged:"
ls -1 "$VENDOR"/nanowakeword-*.whl
echo "Now run: docker compose build"
