#!/bin/bash
# Print the container engine name (docker | podman) for the current host.
# Thin wrapper so Makefile recipes can resolve the engine the same way the
# bin/ scripts and services.py do, instead of assuming `docker`.
source "$(dirname "$0")/_engine.sh"
echo "$ENGINE"
