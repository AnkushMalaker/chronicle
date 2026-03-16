#!/usr/bin/env bash
# Quick deploy havpe-relay on RPi from feat/tailscale-discovery branch
#
# Downloads install.sh to a temp file first so stdin stays on the terminal
# (curl | bash breaks interactive prompts).
export CHRONICLE_HOME=~/my-services/chronicle
TMPFILE=$(mktemp /tmp/chronicle-install.XXXXXX.sh)
curl -sSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/feat/tailscale-discovery/edge/install.sh \
  -o "$TMPFILE"
bash "$TMPFILE" havpe-relay --branch feat/tailscale-discovery
rm -f "$TMPFILE"
