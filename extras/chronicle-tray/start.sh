#!/bin/bash
# macOS: opuslib (pendant extra) needs the Opus shared library on the linker path.
if [ "$(uname)" = "Darwin" ] && command -v brew &>/dev/null; then
    OPUS_PREFIX="$(brew --prefix opus 2>/dev/null)"
    if [ -d "$OPUS_PREFIX/lib" ]; then
        export DYLD_LIBRARY_PATH="${OPUS_PREFIX}/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    fi
fi

cd "$(dirname "$0")"
exec uv run chronicle-tray "$@"
