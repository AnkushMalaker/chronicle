#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
proto_root="$repo_root/contracts/audio/v2/proto"
proto_file="advanced_omi_backend/audio_contract/v2/audio.proto"
python_out="$repo_root/backends/advanced/src"
typescript_out="$repo_root/contracts/audio/v2/typescript"
wakeword_python_out="$repo_root/extras/wakeword-service"
client_python_out="$repo_root/extras/chronicle-client"
generator="${PROTOC_GEN_ES:-$repo_root/node_modules/.bin/protoc-gen-es}"

if [[ ! -x "$generator" ]]; then
  echo "Set PROTOC_GEN_ES to protoc-gen-es v2.14.0" >&2
  exit 2
fi

uv run --with grpcio-tools==1.71.0 python -m grpc_tools.protoc \
  -I"$proto_root" \
  --python_out="$python_out" \
  --pyi_out="$python_out" \
  "$proto_root/$proto_file"

uv run --with grpcio-tools==1.71.0 python -m grpc_tools.protoc \
  -I"$proto_root/advanced_omi_backend" \
  --python_out="$client_python_out" \
  --pyi_out="$client_python_out" \
  "$proto_root/$proto_file"

uv run --with grpcio-tools==1.71.0 python -m grpc_tools.protoc \
  -I"$proto_root/advanced_omi_backend" \
  --python_out="$wakeword_python_out" \
  --pyi_out="$wakeword_python_out" \
  "$proto_root/$proto_file"

uv run --with grpcio-tools==1.71.0 python -m grpc_tools.protoc \
  -I"$proto_root" \
  --plugin="protoc-gen-es=$generator" \
  --es_out="$typescript_out" \
  --es_opt=target=ts \
  "$proto_root/$proto_file"
