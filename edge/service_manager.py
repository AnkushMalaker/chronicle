"""
Service manager agent — start/stop Chronicle services from the WebUI.

A small host-side HTTP API that wraps services.py (the docker compose
orchestrator). Runs natively on the host — like the discovery agent — because
containers can't run docker compose with host bind-mount paths. One agent per
machine; on distributed deployments the backend points SERVICE_MANAGER_URL at
the agent on whichever box hosts the services.

Launched by services.py (any ./start.sh) with:
  SERVICE_MANAGER_TOKEN  — shared secret, required (auto-generated into
                           backends/advanced/.env on first start)
  SERVICE_MANAGER_PORT   — default 8775
  SERVICE_MANAGER_HOST   — default 0.0.0.0 (token-authed; must be reachable
                           from the backend container via host.docker.internal)

All endpoints except /health require "Authorization: Bearer <token>".
Compose operations run in a background thread (builds can take minutes);
POST endpoints return 202 with an operation id to poll.
"""

import io
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from dotenv import dotenv_values, set_key
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from rich.console import Console

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import services  # noqa: E402  (repo-root services.py)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [service-manager] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("SERVICE_MANAGER_TOKEN", "")
PORT = int(os.environ.get("SERVICE_MANAGER_PORT", "8775"))
HOST = os.environ.get("SERVICE_MANAGER_HOST", "0.0.0.0")

VALID_ACTIONS = ("start", "stop", "restart")

# Provider-switchable services: service name → (env file key, provider→compose map)
_PROVIDER_ENV_KEYS = {
    "asr-services": ("ASR_PROVIDER", services.ASR_PROVIDER_TO_SERVICE),
    "tts": ("TTS_PROVIDER", services._TTS_PROVIDER_TO_SERVICE),
}

app = FastAPI(title="Chronicle Service Manager", docs_url=None, redoc_url=None)
_bearer = HTTPBearer(auto_error=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Agent has no token configured")
    if credentials is None or credentials.credentials != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ── Operations: one compose operation at a time, polled by id ────────────────

_ops_lock = threading.Lock()  # guards _operations dict
_busy_lock = threading.Lock()  # serializes compose operations
_operations: dict[str, dict] = {}
_MAX_OPERATIONS = 50


def _record_operation(service: str, action: str) -> dict:
    op = {
        "id": uuid.uuid4().hex[:12],
        "service": service,
        "action": action,
        "status": "running",
        "ok": None,
        "log": "",
        "started_at": time.time(),
        "finished_at": None,
    }
    with _ops_lock:
        _operations[op["id"]] = op
        while len(_operations) > _MAX_OPERATIONS:
            oldest = min(_operations.values(), key=lambda o: o["started_at"])
            if oldest["status"] == "running":
                break
            del _operations[oldest["id"]]
    return op


def _run_operation(op: dict, fn):
    """Run a compose operation in a thread, capturing services.py console output."""

    def _go():
        buf = io.StringIO()
        original_console = services.console
        services.console = Console(file=buf, force_terminal=False, width=120)
        try:
            ok = fn()
            op["ok"] = bool(ok)
            op["status"] = "done" if ok else "failed"
        except Exception as e:
            logger.exception("Operation %s/%s crashed", op["service"], op["action"])
            op["ok"] = False
            op["status"] = "failed"
            buf.write(f"\nException: {e}\n")
        finally:
            services.console = original_console
            op["log"] = buf.getvalue()[-8000:]
            op["finished_at"] = time.time()
            _busy_lock.release()
            logger.info(
                "Operation %s %s → %s", op["action"], op["service"], op["status"]
            )

    threading.Thread(target=_go, daemon=True).start()


def _start_operation(service: str, action: str, fn) -> dict:
    if not _busy_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="Another operation is already running"
        )
    op = _record_operation(service, action)
    logger.info("Operation %s %s started (%s)", action, service, op["id"])
    _run_operation(op, fn)
    return op


# ── Service info ─────────────────────────────────────────────────────────────


def _provider_info(service_name: str) -> dict | None:
    if service_name not in _PROVIDER_ENV_KEYS:
        return None
    env_key, provider_map = _PROVIDER_ENV_KEYS[service_name]
    env_path = REPO_ROOT / services.SERVICES[service_name]["path"] / ".env"
    env_values = dotenv_values(env_path) if env_path.exists() else {}
    current = (env_values.get(env_key) or "").strip("'\"")
    info = {
        "env_key": env_key,
        "current": current,
        "available": [
            {
                "key": key,
                "label": (
                    services._ASR_PROVIDER_LABELS.get(key, key)
                    if service_name == "asr-services"
                    else key
                ),
            }
            for key in provider_map
        ],
    }
    if service_name == "asr-services":
        info["streaming_current"] = (
            env_values.get("STREAMING_ASR_PROVIDER") or ""
        ).strip("'\"")
    return info


def _containers_running(name: str) -> bool:
    """Whether any compose container for this service is currently running."""
    service_path = REPO_ROOT / services.SERVICES[name]["path"]
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--status=running", "-q"],
            cwd=service_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def _service_entry(name: str) -> dict:
    service = services.SERVICES[name]
    health, detail = services.check_service_health(name)
    # "stopped" only means the health endpoint isn't answering. If containers are
    # up, the service is booting (GPU model loads take minutes) — report
    # "starting" so the UI doesn't offer Start again.
    if health == "stopped" and _containers_running(name):
        health = "starting"
        detail = "containers up, waiting for health endpoint"
    return {
        "name": name,
        "description": service["description"],
        "ports": service["ports"],
        "enabled": services.check_service_enabled(name),
        "health": health,
        "health_detail": detail,
        "provider": _provider_info(name),
    }


# ── Routes ───────────────────────────────────────────────────────────────────


class ActionBody(BaseModel):
    build: bool = False
    recreate: bool = False
    force: bool = False


class ProviderBody(BaseModel):
    provider: str
    build: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "host": os.uname().nodename}


@app.get("/services", dependencies=[Depends(require_token)])
def list_services():
    with _ops_lock:
        running = [o for o in _operations.values() if o["status"] == "running"]
    return {
        "services": [_service_entry(name) for name in services.SERVICES],
        "operation": running[0] if running else None,
    }


@app.get("/operations/{op_id}", dependencies=[Depends(require_token)])
def get_operation(op_id: str):
    with _ops_lock:
        op = _operations.get(op_id)
    if not op:
        raise HTTPException(status_code=404, detail="Unknown operation")
    return op


@app.post("/services/{name}/provider", dependencies=[Depends(require_token)])
def set_provider(name: str, body: ProviderBody):
    if name not in _PROVIDER_ENV_KEYS:
        raise HTTPException(
            status_code=400, detail=f"{name} does not support provider switching"
        )
    env_key, provider_map = _PROVIDER_ENV_KEYS[name]
    if body.provider not in provider_map:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{body.provider}'. Available: {', '.join(provider_map)}",
        )

    env_path = REPO_ROOT / services.SERVICES[name]["path"] / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)
    set_key(str(env_path), env_key, body.provider, quote_mode="never")

    was_running = services.check_service_health(name)[0] != "stopped"

    def fn():
        # down first: providers share one port, so the old container must go
        # before the new one binds.
        ok = services.run_compose_command(name, "down")
        if ok and was_running:
            ok = services.run_compose_command(name, "up", build=body.build)
        return ok

    op = _start_operation(name, f"provider:{body.provider}", fn)
    return {"operation": op}


# NOTE: declared after /services/{name}/provider so "provider" never matches {action}.
@app.post("/services/{name}/{action}", dependencies=[Depends(require_token)])
def service_action(name: str, action: str, body: ActionBody | None = None):
    if name not in services.SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
    body = body or ActionBody()

    # Stopping/restarting the backend kills the WebUI (and this request's caller).
    # Require an explicit force flag so it can't happen by accident.
    if name == "backend" and action in ("stop", "restart") and not body.force:
        raise HTTPException(
            status_code=400,
            detail="Stopping the backend kills the WebUI. Pass force=true to confirm.",
        )

    if action == "start":
        if not services.check_service_enabled(name):
            raise HTTPException(
                status_code=400,
                detail=f"{name} is not enabled in config/config.yml — run the wizard first",
            )
        fn = lambda: services.run_compose_command(
            name, "up", build=body.build
        )  # noqa: E731
    elif action == "stop":
        fn = lambda: services.run_compose_command(name, "down")  # noqa: E731
    else:  # restart — down + up so provider/env changes take effect
        fn = lambda: (  # noqa: E731
            services.run_compose_command(name, "down")
            and services.run_compose_command(name, "up", build=body.build)
        )

    op = _start_operation(name, action, fn)
    return {"operation": op}


def main():
    if not TOKEN:
        logger.error("SERVICE_MANAGER_TOKEN not set — refusing to start")
        sys.exit(1)
    logger.info("Service manager listening on %s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
