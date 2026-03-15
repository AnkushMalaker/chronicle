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
BRANCH="main"
REPO_URL="https://github.com/SimpleOpenSoftware/chronicle.git"

# Resolve CHRONICLE_HOME: explicit env var > detect existing clone > default
if [[ -n "${CHRONICLE_HOME:-}" ]]; then
    : # User explicitly set it
elif [[ -d "$HOME/chronicle/.git" ]]; then
    CHRONICLE_HOME="$HOME/chronicle"
elif [[ -d "$PWD/.git" && -d "$PWD/edge" ]]; then
    CHRONICLE_HOME="$PWD"
else
    CHRONICLE_HOME="$HOME/chronicle"
fi

# ── Service → compose path mapping ───────────────────────────────────
declare -A SERVICE_PATHS=(
    [speaker-recognition]=extras/speaker-recognition
    [asr-services]=extras/asr-services
    [tts]=extras/tts
    [llm-services]=extras/llm-services
    [havpe-relay]=extras/havpe-relay
)

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
            for svc in "${!SERVICE_PATHS[@]}"; do echo "  $svc"; done | sort
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

# ── Validate service name ────────────────────────────────────────────
if [[ -z "${SERVICE_PATHS[$SERVICE_NAME]+_}" ]]; then
    err "Unknown service: $SERVICE_NAME"
    err "Available: ${!SERVICE_PATHS[*]}"
    exit 1
fi

COMPOSE_PATH="${SERVICE_PATHS[$SERVICE_NAME]}"

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
if [[ -d "$CHRONICLE_HOME/.git" && -d "$CHRONICLE_HOME/edge" ]]; then
    info "Using existing repo at $CHRONICLE_HOME"
    cd "$CHRONICLE_HOME"
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [[ "$current_branch" != "$BRANCH" ]]; then
        git fetch origin "$BRANCH"
        git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
    fi
    git pull --rebase --autostash origin "$BRANCH" || {
        warn "Could not auto-update (you may have local changes). Continuing with current state."
    }
else
    info "Cloning Chronicle (branch: $BRANCH) to $CHRONICLE_HOME..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CHRONICLE_HOME"
fi
cd "$CHRONICLE_HOME"
ok "Repository ready at $CHRONICLE_HOME"

# ── Resolve compose path ─────────────────────────────────────────────
SERVICE_DIR="$CHRONICLE_HOME/$COMPOSE_PATH"

if [[ ! -d "$SERVICE_DIR" ]]; then
    err "Service directory not found: $SERVICE_DIR"
    exit 1
fi

info "Service directory: $SERVICE_DIR"

# ── Resolve backend URL for edge services ─────────────────────────────
# On edge nodes, host.docker.internal doesn't reach the backend.
# Try minidisc discovery first, then prompt if needed.
info "Looking for Chronicle backend on Tailnet..."
BACKEND_URL=$(cd "$CHRONICLE_HOME" && PYTHONPATH="$CHRONICLE_HOME" uv run --with-requirements "$CHRONICLE_HOME/setup-requirements.txt" python3 -c "
try:
    from discovery import CHRONICLE_BACKEND, discover_service
    url = discover_service(CHRONICLE_BACKEND)
    if url:
        print(url, end='')
    else:
        print('minidisc found no chronicle-backend service on Tailnet', file=__import__('sys').stderr)
except Exception as e:
    print(f'discovery failed: {e}', file=__import__('sys').stderr)
")

if [[ -n "$BACKEND_URL" ]]; then
    ok "Auto-discovered backend at $BACKEND_URL"
else
    warn "Could not auto-discover backend. Make sure your Chronicle backend is running with Tailscale."
    read -rp "[edge] Enter your Chronicle backend URL (e.g. http://100.x.x.x:8000): " BACKEND_URL
    if [[ -z "$BACKEND_URL" ]]; then
        err "Backend URL is required for edge deployment."
        exit 1
    fi
fi

# ── Run init.py if it exists (interactive config) ─────────────────────
INIT_ARGS=""
if [[ -n "$BACKEND_URL" ]]; then
    INIT_ARGS="--backend-url $BACKEND_URL"
fi

INIT_SCRIPT="$SERVICE_DIR/init.py"
if [[ -f "$INIT_SCRIPT" ]]; then
    info "Running configuration wizard for $SERVICE_NAME..."
    cd "$SERVICE_DIR"
    uv run --with-requirements "$CHRONICLE_HOME/setup-requirements.txt" python init.py $INIT_ARGS
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
