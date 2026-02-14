#!/bin/bash

# macOS: opuslib needs the Opus shared library on the dynamic linker path.
# Install with: brew install opus
if [ "$(uname)" = "Darwin" ] && command -v brew &>/dev/null; then
    OPUS_PREFIX="$(brew --prefix opus 2>/dev/null)"
    if [ -d "$OPUS_PREFIX/lib" ]; then
        export DYLD_LIBRARY_PATH="${OPUS_PREFIX}/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    fi
fi

uv run --with-requirements requirements.txt python main.py "$@"
