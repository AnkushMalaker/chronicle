#!/bin/bash
# Shared prerequisite check for Chronicle scripts.
# Source this at the top of any shell script that needs uv.

if ! command -v uv &> /dev/null; then
    echo "❌ 'uv' is not installed."
    echo ""
    echo "Chronicle requires 'uv' (Python package manager) to run."
    echo "Install it with:"
    echo ""
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "Then restart your terminal and try again."
    exit 1
fi
