from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import sqlite3
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    backend_url: str
    source_id: str
    token: str
    screenpipe_dir: Path
    screenpipe_url: str = "http://127.0.0.1:3030"
    screenpipe_token: str | None = None
    poll_seconds: float = 5.0
    activity_debounce_seconds: float = 10.0


class Checkpoints:
    def __init__(self, path: Path):
        self.path = path
        self.values: dict[str, int] = {}
        if path.exists():
            self.values = json.loads(path.read_text(encoding="utf-8"))

    def get(self, stream: str) -> int:
        return int(self.values.get(stream, 0))

    def set(self, stream: str, value: int) -> None:
        self.values[stream] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.values, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def open_screenpipe_db(path: Path) -> sqlite3.Connection:
    """Open the live WAL database without ever requesting a write lock."""
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def infer_audio_direction(path: str) -> str:
    value = path.lower()
    if "(input)" in value or "input" in Path(value).name:
        return "input"
    if "(output)" in value or "output" in Path(value).name:
        return "output"
    return "unknown"


def iso_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value)
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return f"{text}Z"


def timestamp_seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, OSError, ZeroDivisionError):
        return 30.0


def activity_key(row: sqlite3.Row) -> tuple[str, str, str]:
    return (row["app_name"] or "", row["window_name"] or "", row["browser_url"] or "")


def build_activity_sessions(rows: Iterable[sqlite3.Row], debounce_seconds: float = 10.0) -> list[dict[str, Any]]:
    """Collapse frame headers into transitions; OCR and pixels are intentionally ignored."""
    sessions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        key = activity_key(row)
        captured = iso_timestamp(row["timestamp"])
        if current is not None and current["key"] == key:
            current["ended_at"] = captured
            current["last_frame_id"] = row["id"]
            current["frame_count"] += 1
            continue
        if current is not None:
            sessions.append(current)
        current = {
            "key": key,
            "source_item_id": f"activity:{row['id']}",
            "captured_at": captured,
            "ended_at": captured,
            "first_frame_id": row["id"],
            "last_frame_id": row["id"],
            "frame_count": 1,
            "app_name": key[0],
            "window_name": key[1],
            "browser_url": key[2],
            "capture_trigger": row["capture_trigger"] or "",
        }
    if current is not None:
        sessions.append(current)
    return sessions


def fold_activity_rows(rows: Iterable[sqlite3.Row], current: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Extend the open activity across poll boundaries and return closed sessions."""
    closed: list[dict[str, Any]] = []
    for row in rows:
        key = list(activity_key(row))
        captured = iso_timestamp(row["timestamp"])
        if current is not None and current["key"] == key:
            current["ended_at"] = captured
            current["last_frame_id"] = row["id"]
            current["frame_count"] += 1
            continue
        if current is not None:
            closed.append(current)
        current = {
            "key": key,
            "source_item_id": f"activity:{row['id']}",
            "captured_at": captured,
            "ended_at": captured,
            "first_frame_id": row["id"],
            "last_frame_id": row["id"],
            "frame_count": 1,
            "app_name": key[0],
            "window_name": key[1],
            "browser_url": key[2],
            "capture_trigger": row["capture_trigger"] or "",
        }
    return closed, current


class Collector:
    def __init__(self, config: Config, state_dir: Path):
        self.config = config
        self.checkpoints = Checkpoints(state_dir / "checkpoints.json")
        self.activity_path = state_dir / "open_activity.json"
        self.rejections_path = state_dir / "rejections.jsonl"
        self.client = httpx.Client(
            base_url=config.backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=60,
        )

    @property
    def database_path(self) -> Path:
        return self.config.screenpipe_dir / "db.sqlite"

    def heartbeat(self, error: str | None = None) -> None:
        health = {
            "screenpipe_db": str(self.database_path),
            "audio_cursor": self.checkpoints.get("audio"),
            "frame_cursor": self.checkpoints.get("frames"),
        }
        if error:
            health["error"] = error
        response = self.client.post(
            "/api/device-input/heartbeat",
            json={"status": "error" if error else "online", "health": health},
        )
        response.raise_for_status()

    def collect_audio(self, connection: sqlite3.Connection) -> int:
        columns = table_columns(connection, "audio_chunks")
        required = {"id", "file_path", "timestamp"}
        if not required <= columns:
            # ScreenPipe may expose a migration placeholder briefly before the
            # first audio segment initializes the final schema.
            count = connection.execute("SELECT COUNT(*) FROM audio_chunks").fetchone()[0]
            if count == 0:
                return 0
            raise RuntimeError(f"unsupported ScreenPipe audio_chunks schema; missing {sorted(required - columns)}")
        cursor = self.checkpoints.get("audio")
        rows = connection.execute(
            "SELECT id, file_path, timestamp FROM audio_chunks WHERE id > ? AND timestamp IS NOT NULL ORDER BY id LIMIT 100",
            (cursor,),
        ).fetchall()
        sent = 0
        for row in rows:
            path = Path(row["file_path"])
            if not path.is_file():
                captured = timestamp_seconds(iso_timestamp(row["timestamp"]))
                if time.time() - captured < 120:
                    logger.warning("audio chunk %s is not available yet: %s", row["id"], path)
                    break
                self.rejections_path.parent.mkdir(parents=True, exist_ok=True)
                with self.rejections_path.open("a", encoding="utf-8") as rejected:
                    rejected.write(json.dumps({"stream": "audio", "source_item_id": row["id"], "detail": "source media missing"}) + "\n")
                self.checkpoints.set("audio", row["id"])
                continue
            before = path.stat()
            time.sleep(0.05)
            after = path.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                break
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            content_type = mimetypes.guess_type(path.name)[0] or "audio/wav"
            with path.open("rb") as handle:
                response = self.client.post(
                    "/api/device-input/audio",
                    data={
                        "source_item_id": str(row["id"]),
                        "captured_at": iso_timestamp(row["timestamp"]),
                        "duration_seconds": str(audio_duration(path)),
                        "device_name": path.stem,
                        "direction": infer_audio_direction(str(path)),
                        "content_hash": digest,
                    },
                    files={"file": (path.name, handle, content_type)},
                )
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code >= 400:
                logger.error("audio chunk %s rejected: %s", row["id"], response.text)
                self.rejections_path.parent.mkdir(parents=True, exist_ok=True)
                with self.rejections_path.open("a", encoding="utf-8") as rejected:
                    rejected.write(json.dumps({"stream": "audio", "source_item_id": row["id"], "status": response.status_code, "detail": response.text[:1000]}) + "\n")
            self.checkpoints.set("audio", row["id"])
            sent += 1
        return sent

    def collect_activity(self, connection: sqlite3.Connection) -> int:
        columns = table_columns(connection, "frames")
        required = {"id", "timestamp", "app_name", "window_name"}
        if not required <= columns:
            raise RuntimeError(f"unsupported ScreenPipe frames schema; missing {sorted(required - columns)}")
        optional = lambda name: name if name in columns else f"NULL AS {name}"
        cursor = self.checkpoints.get("frames")
        rows = connection.execute(
            f"SELECT id, timestamp, app_name, window_name, {optional('browser_url')}, {optional('capture_trigger')} "
            "FROM frames WHERE id > ? ORDER BY id LIMIT 1000",
            (cursor,),
        ).fetchall()
        if not rows:
            return 0
        current = None
        if self.activity_path.exists():
            current = json.loads(self.activity_path.read_text(encoding="utf-8"))
        closed, current = fold_activity_rows(rows, current)
        sessions = [
            session
            for session in ([*closed, current] if current else closed)
            if timestamp_seconds(session["ended_at"]) - timestamp_seconds(session["captured_at"])
            >= self.config.activity_debounce_seconds
        ]
        if not sessions:
            self.activity_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.activity_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(current), encoding="utf-8")
            temporary.replace(self.activity_path)
            self.checkpoints.set("frames", rows[-1]["id"])
            return 0
        payload = {
            "items": [
                {
                    "source_item_id": session["source_item_id"],
                    "captured_at": session["captured_at"],
                    "ended_at": session["ended_at"],
                    "metadata": {k: v for k, v in session.items() if k not in {"key", "source_item_id", "captured_at", "ended_at"}},
                }
                for session in sessions
            ]
        }
        response = self.client.post("/api/device-input/activity", json=payload)
        response.raise_for_status()
        self.activity_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.activity_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        temporary.replace(self.activity_path)
        self.checkpoints.set("frames", rows[-1]["id"])
        return len(payload["items"])

    def process_job(self) -> bool:
        response = self.client.get("/api/device-input/jobs/next")
        response.raise_for_status()
        job = response.json().get("job")
        if not job:
            return False
        try:
            raw_items = []
            offset = 0
            page_size = 500
            while True:
                params = {
                    "content_type": "ocr",
                    "start_time": iso_timestamp(job["start_at"])
                    if job.get("start_at")
                    else None,
                    "end_time": iso_timestamp(job["end_at"])
                    if job.get("end_at")
                    else None,
                    "limit": page_size,
                    "offset": offset,
                }
                headers = (
                    {"Authorization": f"Bearer {self.config.screenpipe_token}"}
                    if self.config.screenpipe_token
                    else None
                )
                local = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/search",
                    params=params,
                    headers=headers,
                    timeout=60,
                )
                local.raise_for_status()
                page = local.json().get("data", [])
                raw_items.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
            items = []
            for raw in raw_items:
                content = raw.get("content", raw)
                frame_id = content.get("frame_id")
                if frame_id is None:
                    continue
                items.append({
                    "source_item_id": f"frame:{frame_id}",
                    "captured_at": content.get("timestamp"),
                    "metadata": {
                        "frame_id": frame_id,
                        "app_name": content.get("app_name"),
                        "window_name": content.get("window_name"),
                        "browser_url": content.get("browser_url"),
                        "text": content.get("text"),
                    },
                })
            result = {"success": True, "items": items}
        except Exception as exc:
            result = {"success": False, "items": [], "error": str(exc)}
        done = self.client.post(f"/api/device-input/jobs/{job['id']}/complete", json=result)
        done.raise_for_status()
        return True

    def run(self) -> None:
        last_heartbeat = 0.0
        while True:
            try:
                with open_screenpipe_db(self.database_path) as connection:
                    self.collect_audio(connection)
                    self.collect_activity(connection)
                self.process_job()
                if time.monotonic() - last_heartbeat >= 30:
                    self.heartbeat()
                    last_heartbeat = time.monotonic()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logger.exception("collector pass failed")
                try:
                    self.heartbeat(str(exc))
                except Exception:
                    logger.exception("heartbeat failed")
            try:
                time.sleep(self.config.poll_seconds)
            except KeyboardInterrupt:
                return
