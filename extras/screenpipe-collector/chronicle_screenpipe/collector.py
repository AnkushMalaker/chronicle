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
from typing import Any

import httpx

from .observations import ObservationTracker

logger = logging.getLogger(__name__)

# SQLite's default compiled limit on host parameters in one statement is 999.
_SQLITE_PARAMETER_LIMIT = 500


@dataclass(frozen=True)
class Config:
    backend_url: str
    source_id: str
    token: str
    screenpipe_dir: Path
    screenpipe_url: str = "http://127.0.0.1:3030"
    screenpipe_token: str | None = None
    forward_audio: str = "both"
    # Word-overlap threshold handed to ScreenPipe's `/search?dedupe=`, which
    # collapses consecutive near-identical frames before they cross the wire.
    # Matches the backend's own default so a recorder that predates the
    # parameter — it is ignored there — filters to the same result later.
    # Set to None to receive every frame.
    search_dedupe: float | None = 0.85
    poll_seconds: float = 5.0
    activity_debounce_seconds: float = 10.0
    sample_cooldown_seconds: float = 120.0
    liveness_seconds: float = 900.0


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


class Collector:
    def __init__(self, config: Config, state_dir: Path):
        self.config = config
        self.checkpoints = Checkpoints(state_dir / "checkpoints.json")
        self.observations_path = state_dir / "observations.json"
        self.rejections_path = state_dir / "rejections.jsonl"
        self.metrics = {
            "observation_opens": 0,
            "observation_closes": 0,
            "observation_samples": 0,
        }
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
            **self.metrics,
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
            count = connection.execute("SELECT COUNT(*) FROM audio_chunks").fetchone()[
                0
            ]
            if count == 0:
                return 0
            raise RuntimeError(
                f"unsupported ScreenPipe audio_chunks schema; missing {sorted(required - columns)}"
            )
        cursor = self.checkpoints.get("audio")
        rows = connection.execute(
            "SELECT id, file_path, timestamp FROM audio_chunks WHERE id > ? AND timestamp IS NOT NULL ORDER BY id LIMIT 100",
            (cursor,),
        ).fetchall()
        sent = 0
        for row in rows:
            path = Path(row["file_path"])
            direction = infer_audio_direction(str(path))
            if self.config.forward_audio == "none" or (
                self.config.forward_audio != "both"
                and direction != self.config.forward_audio
            ):
                self.checkpoints.set("audio", row["id"])
                continue
            if not path.is_file():
                captured = timestamp_seconds(iso_timestamp(row["timestamp"]))
                if time.time() - captured < 120:
                    logger.warning(
                        "audio chunk %s is not available yet: %s", row["id"], path
                    )
                    break
                self.rejections_path.parent.mkdir(parents=True, exist_ok=True)
                with self.rejections_path.open("a", encoding="utf-8") as rejected:
                    rejected.write(
                        json.dumps(
                            {
                                "stream": "audio",
                                "source_item_id": row["id"],
                                "detail": "source media missing",
                            }
                        )
                        + "\n"
                    )
                self.checkpoints.set("audio", row["id"])
                continue
            before = path.stat()
            time.sleep(0.05)
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
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
                        "direction": direction,
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
                    rejected.write(
                        json.dumps(
                            {
                                "stream": "audio",
                                "source_item_id": row["id"],
                                "status": response.status_code,
                                "detail": response.text[:1000],
                            }
                        )
                        + "\n"
                    )
            self.checkpoints.set("audio", row["id"])
            sent += 1
        return sent

    def _load_observation_tracker(self) -> ObservationTracker:
        state = None
        if self.observations_path.exists():
            state = json.loads(self.observations_path.read_text(encoding="utf-8"))
        return ObservationTracker(
            state,
            stability_seconds=self.config.activity_debounce_seconds,
            sample_cooldown_seconds=self.config.sample_cooldown_seconds,
            liveness_seconds=self.config.liveness_seconds,
        )

    def _save_observation_tracker(self, tracker: ObservationTracker) -> None:
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.observations_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(tracker.state, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(self.observations_path)

    def _send_observation_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        response = self.client.post(
            "/api/device-input/observations", json={"events": events}
        )
        response.raise_for_status()
        for event in events:
            key = {
                "open": "observation_opens",
                "close": "observation_closes",
                "sample": "observation_samples",
            }[event["event"]]
            self.metrics[key] += 1
        return len(events)

    def collect_observations(self, connection: sqlite3.Connection) -> int:
        columns = table_columns(connection, "frames")
        required = {"id", "timestamp", "app_name", "window_name"}
        if not required <= columns:
            raise RuntimeError(
                f"unsupported ScreenPipe frames schema; missing {sorted(required - columns)}"
            )

        def optional(name: str) -> str:
            return name if name in columns else f"NULL AS {name}"

        cursor = self.checkpoints.get("frames")
        rows = connection.execute(
            f"SELECT id, timestamp, app_name, window_name, {optional('browser_url')}, "
            f"{optional('capture_trigger')}, {optional('full_text')}, {optional('text_source')} "
            "FROM frames WHERE id > ? ORDER BY id LIMIT 1000",
            (cursor,),
        ).fetchall()
        tracker = self._load_observation_tracker()
        now = datetime.now(timezone.utc).isoformat()
        events = tracker.process_rows(rows, now)
        sent = self._send_observation_events(events)
        self._save_observation_tracker(tracker)
        if rows:
            self.checkpoints.set("frames", rows[-1]["id"])
        return sent

    def close_observation(self) -> int:
        tracker = self._load_observation_tracker()
        events = tracker.close(datetime.now(timezone.utc).isoformat())
        sent = self._send_observation_events(events)
        self._save_observation_tracker(tracker)
        return sent

    def _attach_capture_triggers(self, items: list[dict[str, Any]]) -> None:
        """Add ScreenPipe's own reason for capturing each frame.

        `/search` does not expose `capture_trigger`, so it comes from the local
        database instead. It tells Chronicle which frames were explicit capture
        events — a manual grab or a window focus — rather than incidental
        samples, so those are never folded into a neighbouring frame. Best
        effort: a frame with no trigger is simply left unlabelled.
        """
        by_frame = {
            item["metadata"]["frame_id"]: item
            for item in items
            if item["metadata"].get("frame_id") is not None
        }
        if not by_frame:
            return
        frame_ids = list(by_frame)
        try:
            with open_screenpipe_db(self.database_path) as connection:
                for start in range(0, len(frame_ids), _SQLITE_PARAMETER_LIMIT):
                    batch = frame_ids[start : start + _SQLITE_PARAMETER_LIMIT]
                    placeholders = ",".join("?" * len(batch))
                    rows = connection.execute(
                        f"SELECT id, capture_trigger FROM frames WHERE id IN ({placeholders})",
                        batch,
                    ).fetchall()
                    for row in rows:
                        if row["capture_trigger"]:
                            by_frame[row["id"]]["metadata"]["capture_trigger"] = row[
                                "capture_trigger"
                            ]
        except sqlite3.Error:
            logger.warning("could not read capture triggers", exc_info=True)

    def process_job(self) -> bool:
        response = self.client.get("/api/device-input/jobs/next")
        response.raise_for_status()
        job = response.json().get("job")
        if not job:
            return False
        try:
            if job["kind"] in {"thumbnail", "source_media"}:
                frame_id = job.get("payload", {}).get("frame_id")
                if frame_id is None:
                    raise RuntimeError("thumbnail job is missing frame_id")
                headers = (
                    {"Authorization": f"Bearer {self.config.screenpipe_token}"}
                    if self.config.screenpipe_token
                    else None
                )
                width = int(job.get("payload", {}).get("width") or 640)
                quality = 85 if width > 640 else 75
                thumbnail = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/frames/{frame_id}/thumbnail",
                    params={"width": width, "quality": quality},
                    headers=headers,
                    timeout=30,
                )
                thumbnail.raise_for_status()
                done = self.client.post(
                    f"/api/device-input/jobs/{job['id']}/thumbnail",
                    files={
                        "file": (
                            f"screenpipe-frame-{frame_id}.jpg",
                            thumbnail.content,
                            thumbnail.headers.get("content-type", "image/jpeg"),
                        )
                    },
                )
                done.raise_for_status()
                return True
            raw_items = []
            offset = 0
            page_size = 500
            collapsed = 0
            while True:
                params = {
                    "content_type": "ocr",
                    "start_time": (
                        iso_timestamp(job["start_at"]) if job.get("start_at") else None
                    ),
                    "end_time": (
                        iso_timestamp(job["end_at"]) if job.get("end_at") else None
                    ),
                    "limit": page_size,
                    "offset": offset,
                }
                if self.config.search_dedupe:
                    params["dedupe"] = self.config.search_dedupe
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
                body = local.json()
                page = body.get("data", [])
                # `limit` bounds rows scanned, not returned, so a deduplicated
                # page is short of `page_size` while more data remains. Page on
                # rows *scanned* instead. A recorder without the parameter
                # reports no `deduped`, which reduces this to the plain
                # short-page test.
                deduped = int(body.get("deduped") or 0)
                collapsed += deduped
                scanned = len(page) + deduped
                raw_items.extend(page)
                if scanned < page_size:
                    break
                offset += scanned
            if collapsed:
                logger.info(
                    "screenpipe collapsed %d near-duplicate frames for job %s",
                    collapsed,
                    job["id"],
                )
            items = []
            for raw in raw_items:
                content = raw.get("content", raw)
                frame_id = content.get("frame_id")
                if frame_id is None:
                    continue
                items.append(
                    {
                        "source_item_id": f"frame:{frame_id}",
                        "captured_at": content.get("timestamp"),
                        "metadata": {
                            "frame_id": frame_id,
                            "app_name": content.get("app_name"),
                            "window_name": content.get("window_name"),
                            "browser_url": content.get("browser_url"),
                            "text": content.get("text"),
                            # How ScreenPipe read this frame. Chronicle needs it to
                            # tell a dense accessibility-tree read from the OCR
                            # fallback used where a window exposes no tree.
                            "text_source": content.get("text_source"),
                        },
                    }
                )
            self._attach_capture_triggers(items)
            result = {"success": True, "items": items}
        except Exception as exc:
            result = {"success": False, "items": [], "error": str(exc)}
        done = self.client.post(
            f"/api/device-input/jobs/{job['id']}/complete", json=result
        )
        done.raise_for_status()
        return True

    def run(self) -> None:
        last_heartbeat = 0.0
        while True:
            try:
                with open_screenpipe_db(self.database_path) as connection:
                    self.collect_audio(connection)
                    self.collect_observations(connection)
                self.process_job()
                if time.monotonic() - last_heartbeat >= 30:
                    self.heartbeat()
                    last_heartbeat = time.monotonic()
            except KeyboardInterrupt:
                try:
                    self.close_observation()
                except Exception:
                    logger.exception("failed to close observation during shutdown")
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
                try:
                    self.close_observation()
                except Exception:
                    logger.exception("failed to close observation during shutdown")
                return
