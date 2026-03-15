#!/usr/bin/env bash
# Quick deploy havpe-relay on RPi from feat/tailscale-discovery branch
CHRONICLE_HOME=~/my-services/chronicle \
  curl -sSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/feat/tailscale-discovery/edge/install.sh \
  | bash -s -- havpe-relay --branch feat/tailscale-discovery
