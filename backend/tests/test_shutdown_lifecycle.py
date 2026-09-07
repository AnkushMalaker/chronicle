import asyncio
import multiprocessing
import os
import signal
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from backend.routers.modules import sse_routes
from backend.server import create_server
from backend.workers.orchestrator import (
    OrchestratorConfig,
    ProcessManager,
    WorkerDefinition,
    WorkerType,
)


def _free_tcp_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_sse_server(port: int, lifespan_stopped, redis_closed) -> None:
    class FakePubSub:
        async def subscribe(self, _channel):
            return None

        async def get_message(self, **_kwargs):
            await asyncio.sleep(0.05)
            return None

        async def unsubscribe(self, _channel):
            return None

        async def aclose(self):
            return None

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

        async def aclose(self):
            redis_closed.set()

    async def authenticated_user(_token):
        return SimpleNamespace(id="test-user")

    sse_routes.create_async_redis = lambda **_kwargs: FakeRedis()
    sse_routes.get_user_from_token_param = authenticated_user

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            lifespan_stopped.set()

    app = FastAPI(lifespan=lifespan)
    app.include_router(sse_routes.router, prefix="/api")

    create_server(app, host="127.0.0.1", port=port).run()


def _open_sse(port: int) -> socket.socket:
    deadline = time.monotonic() + 5
    while True:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)

    connection.settimeout(2)
    connection.sendall(
        b"GET /api/events/stream?token=test HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )
    response = b""
    while b"event: connected" not in response:
        response += connection.recv(4096)
    return connection


def _run_stubborn_stream_server(port: int, lifespan_stopped) -> None:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            lifespan_stopped.set()

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/events/stream")
    async def stream():
        async def events():
            yield "event: connected\ndata: {}\n\n"
            while True:
                await asyncio.sleep(60)

        return StreamingResponse(events(), media_type="text/event-stream")

    create_server(app, host="127.0.0.1", port=port).run()


def test_backend_shutdown_cancels_persistent_sse_before_container_deadline():
    context = multiprocessing.get_context("fork")
    lifespan_stopped = context.Event()
    redis_closed = context.Event()
    port = _free_tcp_port()
    process = context.Process(
        target=_run_sse_server, args=(port, lifespan_stopped, redis_closed)
    )
    process.start()

    connection = None
    try:
        connection = _open_sse(port)
        started = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        process.join(timeout=5)
        elapsed = time.monotonic() - started

        assert not process.is_alive(), "server exceeded its bounded shutdown window"
        assert lifespan_stopped.is_set(), "application lifespan cleanup did not run"
        assert redis_closed.is_set(), "SSE Redis subscription was not closed"
        assert elapsed < 2
    finally:
        if connection is not None:
            connection.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=2)


def test_backend_shutdown_forces_a_stuck_request_before_container_deadline():
    context = multiprocessing.get_context("fork")
    lifespan_stopped = context.Event()
    port = _free_tcp_port()
    process = context.Process(
        target=_run_stubborn_stream_server, args=(port, lifespan_stopped)
    )
    process.start()

    connection = None
    try:
        connection = _open_sse(port)
        started = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        process.join(timeout=5)
        elapsed = time.monotonic() - started

        assert not process.is_alive(), "stuck request exceeded the shutdown backstop"
        assert lifespan_stopped.is_set(), "application lifespan cleanup did not run"
        assert elapsed < 4
    finally:
        if connection is not None:
            connection.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=2)


_DELAYED_SIGTERM_WORKER = """
import signal
import sys
import time
from pathlib import Path

def stop(_signum, _frame):
    time.sleep(0.25)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1]).touch()
while True:
    time.sleep(60)
"""


def test_worker_fleet_shutdown_uses_one_shared_deadline(tmp_path: Path):
    ready_files = [tmp_path / f"worker-{index}.ready" for index in range(6)]
    manager = ProcessManager(
        [
            WorkerDefinition(
                name=f"worker-{index}",
                command=[sys.executable, "-c", _DELAYED_SIGTERM_WORKER, str(ready)],
            )
            for index, ready in enumerate(ready_files)
        ]
    )
    assert manager.start_all()

    deadline = time.monotonic() + 3
    while not all(ready.exists() for ready in ready_files):
        assert time.monotonic() < deadline, "fixture workers did not become ready"
        time.sleep(0.01)

    started = time.monotonic()
    assert manager.stop_all(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1, "fleet shutdown multiplied the delay by worker count"
    assert all(not worker.is_alive for worker in manager.get_all_workers())


def test_container_shutdown_policy_has_internal_headroom(monkeypatch):
    monkeypatch.delenv("WORKER_SHUTDOWN_TIMEOUT", raising=False)
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert compose["services"]["chronicle-backend"]["stop_grace_period"] == "15s"
    assert compose["services"]["workers"]["stop_grace_period"] == "15s"
    assert OrchestratorConfig().shutdown_timeout == 8


_BUSY_RQ_WORKER = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
signals = 0
first_signal_at = None

def stop(_signum, _frame):
    global signals, first_signal_at
    signals += 1
    now = time.monotonic()
    if signals == 1:
        first_signal_at = now
        return
    # RQ ignores duplicate shutdown signals received less than one second apart.
    if now - first_signal_at < 1:
        return
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(60)
"""


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_busy_rq_worker_gets_cold_shutdown_before_hard_kill(tmp_path: Path):
    child_pid_path = tmp_path / "horse.pid"
    manager = ProcessManager(
        [
            WorkerDefinition(
                name="busy-rq-worker",
                command=[sys.executable, "-c", _BUSY_RQ_WORKER, str(child_pid_path)],
                worker_type=WorkerType.RQ_WORKER,
            )
        ]
    )
    assert manager.start_all()

    deadline = time.monotonic() + 3
    while not child_pid_path.exists():
        assert time.monotonic() < deadline, "fixture RQ worker did not become ready"
        time.sleep(0.01)
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    try:
        started = time.monotonic()
        assert manager.stop_all(timeout=1.2)
        elapsed = time.monotonic() - started

        assert elapsed < 2.5
        assert not _pid_exists(child_pid), "RQ work horse survived its parent"
    finally:
        if _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
