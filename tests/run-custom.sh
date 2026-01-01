#!/bin/bash
# Quick wrapper for running Robot tests with custom configs
# Usage: ./run-custom.sh <config-name> [parakeet-url]
#
# Examples:
#   ./run-custom.sh parakeet-openai http://host.docker.internal:8767
#   ./run-custom.sh deepgram-openai
#   ./run-custom.sh parakeet-ollama http://host.docker.internal:8767

set -e

CONFIG_NAME="${1:-parakeet-openai}"
PARAKEET_URL="${2:-http://host.docker.internal:8767}"

echo "Running Robot tests with config: ${CONFIG_NAME}"
echo "Parakeet ASR URL: ${PARAKEET_URL}"

CONFIG_FILE="../tests/configs/${CONFIG_NAME}.yml" \
  PARAKEET_ASR_URL="${PARAKEET_URL}" \
  ./run-robot-tests.sh
