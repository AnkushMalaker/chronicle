#!/usr/bin/env bash
# Chronicle Edge — one-liner service deployment on remote nodes.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/.../edge/install.sh | bash -s -- speaker-recognition
#   curl -sSL ... | bash -s -- asr-services --branch dev
#
# Prerequisites: docker (with compose), tailscale (connected), uv, git
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/.chronicle}"
BRANCH="main"
REPO_URL="https://github.com/SimpleOpenSoftware/chronicle.git"

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${CYAN}[edge]${NC} $*"; }
ok()    { echo -e "${GREEN}[edge]${NC} $*"; }
warn()  { echo -e "${YELLOW}[edge]${NC} $*"; }
err()   { echo -e "${RED}[edge]${NC} $*" >&2; }

# ── Parse args ────────────────────────────────────────────────────────
SERVICE_NAME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --repo)   REPO_URL="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 <service-name> [--branch <branch>] [--repo <url>]"
            echo ""
            echo "Available services:"
            echo "  speaker-recognition   Speaker Recognition Service"
            echo "  asr-services          ASR Speech-to-Text Service"
            echo "  tts                   Text-to-Speech Service"
            echo "  llm-services          Local LLM via llama.cpp"
            echo "  havpe-relay           HAVPE Audio Relay"
            exit 0
            ;;
        -*) err "Unknown option: $1"; exit 1 ;;
        *)  SERVICE_NAME="$1"; shift ;;
    esac
done

if [[ -z "$SERVICE_NAME" ]]; then
    err "Service name required. Run with --help for usage."
    exit 1
fi

# ── Validate service name against registry ────────────────────────────
# We'll validate after clone, but do a quick sanity check here.
KNOWN_SERVICES="speaker-recognition asr-services tts llm-services havpe-relay"
if ! echo "$KNOWN_SERVICES" | grep -qw "$SERVICE_NAME"; then
    err "Unknown service: $SERVICE_NAME"
    err "Available: $KNOWN_SERVICES"
    exit 1
fi

# ── Check prerequisites ──────────────────────────────────────────────
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        err "Required command not found: $1"
        err "Please install $1 before running this script."
        exit 1
    fi
}

info "Checking prerequisites..."
check_cmd docker
check_cmd git
check_cmd uv

# Check docker compose (plugin or standalone)
if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    err "docker compose not found. Install Docker Compose."
    exit 1
fi

# Check Tailscale
if ! command -v tailscale &>/dev/null; then
    err "Tailscale not found. Install from https://tailscale.com/download"
    exit 1
fi

if ! tailscale status &>/dev/null; then
    err "Tailscale is not connected. Run: sudo tailscale up"
    exit 1
fi

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
ok "Prerequisites OK (Tailscale IP: $TAILSCALE_IP)"

# ── Clone / update repo ──────────────────────────────────────────────
if [[ -d "$CHRONICLE_HOME/.git" ]]; then
    info "Updating existing clone at $CHRONICLE_HOME..."
    cd "$CHRONICLE_HOME"
    git fetch origin "$BRANCH" --depth 1
    git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    info "Cloning Chronicle (branch: $BRANCH) to $CHRONICLE_HOME..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CHRONICLE_HOME"
fi
cd "$CHRONICLE_HOME"
ok "Repository ready at $CHRONICLE_HOME"

# ── Read service metadata from registry ───────────────────────────────
# Parse compose_path from edge/services.yml using Python (available via uv)
COMPOSE_PATH=$(uv run python3 -c "
import yaml, sys
with open('edge/services.yml') as f:
    data = yaml.safe_load(f)
svc = data.get('services', {}).get('$SERVICE_NAME')
if not svc:
    print('NOT_FOUND', file=sys.stderr); sys.exit(1)
print(svc['compose_path'])
")

if [[ -z "$COMPOSE_PATH" || ! -d "$COMPOSE_PATH" ]]; then
    err "Service directory not found: $COMPOSE_PATH"
    exit 1
fi

SERVICE_DIR="$CHRONICLE_HOME/$COMPOSE_PATH"
info "Service directory: $SERVICE_DIR"

# ── Run init.py if it exists (interactive config) ─────────────────────
INIT_SCRIPT="$SERVICE_DIR/init.py"
if [[ -f "$INIT_SCRIPT" ]]; then
    info "Running configuration wizard for $SERVICE_NAME..."
    cd "$SERVICE_DIR"
    uv run --with-requirements "$CHRONICLE_HOME/setup-requirements.txt" python init.py
    cd "$CHRONICLE_HOME"
elif [[ -f "$SERVICE_DIR/setup.sh" ]]; then
    info "Running setup script for $SERVICE_NAME..."
    cd "$SERVICE_DIR"
    bash setup.sh
    cd "$CHRONICLE_HOME"
else
    warn "No init script found — using defaults."
fi

# ── Create Docker network ─────────────────────────────────────────────
docker network create chronicle-network 2>/dev/null || true

# ── Start service + edge agent ────────────────────────────────────────
cd "$SERVICE_DIR"
info "Starting $SERVICE_NAME + edge agent..."

# Determine GPU profile if applicable (same logic as services.py)
PROFILES="--profile edge"
if [[ -f .env ]]; then
    PYTORCH_VERSION=$(grep -s '^PYTORCH_CUDA_VERSION=' .env | cut -d= -f2 | tr -d "'" | tr -d '"' || echo "")
    if [[ "$PYTORCH_VERSION" == "strixhalo" ]]; then
        PROFILES="$PROFILES --profile strixhalo"
    elif [[ "$PYTORCH_VERSION" == cu* ]]; then
        PROFILES="$PROFILES --profile gpu"
    elif [[ -n "$PYTORCH_VERSION" ]]; then
        PROFILES="$PROFILES --profile cpu"
    fi
fi

$COMPOSE $PROFILES up --build -d

ok "────────────────────────────────────────"
ok "  $SERVICE_NAME is running!"
ok ""
ok "  Tailscale IP:  $TAILSCALE_IP"
ok "  Service dir:   $SERVICE_DIR"
ok ""
ok "  Check status:  cd $SERVICE_DIR && $COMPOSE ps"
ok "  View logs:     cd $SERVICE_DIR && $COMPOSE logs -f"
ok "  Stop:          cd $SERVICE_DIR && $COMPOSE $PROFILES down"
ok ""
ok "  The service should appear on the Network page"
ok "  of your Chronicle backend dashboard."
ok "────────────────────────────────────────"
