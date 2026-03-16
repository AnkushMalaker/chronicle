#!/bin/bash
# Flash default HA-style firmware for A/B audio comparison.
#
# Usage:
#   ./flash_default.sh           # compile and flash
#   ./flash_default.sh logs      # view device logs
#
# After flashing:
#   uv run python capture_default_audio.py <device_ip>
#   Hold the center button to stream audio, release to stop.

set -e
cd "$(dirname "$0")/firmware"

if [ ! -f secrets.yaml ]; then
    echo "Error: firmware/secrets.yaml not found."
    echo "Run ./init.sh and enable firmware setup, or:"
    echo "  cp secrets.template.yaml secrets.yaml"
    echo "  # then edit secrets.yaml with your WiFi and relay IP"
    exit 1
fi

ACTION="${1:-run}"
cd ..
exec uv run --group firmware esphome "$ACTION" firmware/voice-default.yaml
