"""Read-only access to a ScreenPipe capture archive.

The archive is the local source of truth: SQLite rows for frame metadata, OCR and
accessibility text, plus H.264 chunks holding the pixels. Nothing in this module
writes to the archive.

Frames are addressed by ``offset_index`` (the decoded frame number inside its
chunk), never by seeking to a timestamp. ScreenPipe chunks have fractional and
variable frame rates -- ``compact_monitor_33`` reports 29/155 fps -- so a
timestamp seek lands on a neighbouring frame and the returned pixels disagree
with the stored OCR. Index addressing is exact.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

DEFAULT_ARCHIVE = Path(os.environ.get("SCREENPIPE_DIR", Path.home() / ".screenpipe"))
CACHE_DIR = Path(
    os.environ.get(
        "SCREEN_MEMORY_LAB_CACHE", Path(__file__).resolve().parents[1] / "out" / "cache"
    )
)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Frame:
    id: int
    timestamp: datetime
    app_name: str
    window_name: str
    browser_url: str
    capture_trigger: str
    text_source: str
    ocr_text: str
    accessibility_text: str
    content_hash: int | None
    simhash: int | None
    offset_index: int
    chunk_path: str | None
    focused: bool | None

    @property
    def text(self) -> str:
        """The frame's best available text, preferring OCR of what was on screen."""
        return self.ocr_text or self.accessibility_text or ""

    @property
    def local_time(self) -> datetime:
        return self.timestamp.astimezone(IST)

    @property
    def context(self) -> str:
        parts = [p for p in (self.app_name, self.window_name) if p]
        return " | ".join(parts) if parts else "(no context)"

    def as_row(self) -> dict:
        return {
            "frame_id": self.id,
            "utc": self.timestamp.isoformat(),
            "local": self.local_time.strftime("%Y-%m-%d %H:%M:%S"),
            "app": self.app_name,
            "window": self.window_name,
            "trigger": self.capture_trigger,
            "text_source": self.text_source,
            "chars": len(self.text),
        }


def parse_ts(raw: str) -> datetime:
    """Parse a ScreenPipe timestamp, which carries nanosecond precision."""
    cleaned = raw.strip()
    match = re.match(r"^(.*?\.\d{1,6})\d*([+-]\d{2}:\d{2}|Z)?$", cleaned)
    if match:
        cleaned = match.group(1) + (match.group(2) or "+00:00")
    elif cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Archive:
    """A read-only view over one ScreenPipe data directory."""

    def __init__(self, root: Path | str = DEFAULT_ARCHIVE):
        self.root = Path(root)
        db_path = self.root / "db.sqlite"
        if not db_path.exists():
            raise FileNotFoundError(f"no ScreenPipe database at {db_path}")
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------- frames

    _SELECT = """
        SELECT f.id, f.timestamp, f.offset_index, f.focused,
               COALESCE(f.app_name, '')            AS app_name,
               COALESCE(f.window_name, '')         AS window_name,
               COALESCE(f.browser_url, '')         AS browser_url,
               COALESCE(f.capture_trigger, '')     AS capture_trigger,
               COALESCE(f.text_source, '')         AS text_source,
               COALESCE(f.full_text, '')           AS full_text,
               COALESCE(f.accessibility_text, '')  AS accessibility_text,
               f.content_hash, f.simhash,
               vc.file_path                        AS chunk_path
        FROM frames f
        LEFT JOIN video_chunks vc ON vc.id = f.video_chunk_id
    """

    def _rows_to_frames(self, rows) -> list[Frame]:
        out = []
        for r in rows:
            ocr = r["full_text"]
            acc = r["accessibility_text"]
            # ScreenPipe stores the winning text in full_text; text_source says
            # where it came from. Keep the two apart so callers can reason about
            # provenance (OCR of pixels vs. accessibility tree, which leaks
            # background tabs and browser chrome).
            if r["text_source"] == "accessibility":
                ocr, acc = "", ocr or acc
            out.append(
                Frame(
                    id=r["id"],
                    timestamp=parse_ts(r["timestamp"]),
                    app_name=r["app_name"],
                    window_name=r["window_name"],
                    browser_url=r["browser_url"],
                    capture_trigger=r["capture_trigger"],
                    text_source=r["text_source"],
                    ocr_text=ocr,
                    accessibility_text=acc,
                    content_hash=r["content_hash"],
                    simhash=r["simhash"],
                    offset_index=r["offset_index"],
                    chunk_path=r["chunk_path"],
                    focused=r["focused"],
                )
            )
        return out

    def frames(
        self, start: str | datetime, end: str | datetime, limit: int | None = None
    ) -> list[Frame]:
        """Frames whose timestamp falls in ``[start, end)``, oldest first.

        Bounds are compared as ISO strings against the stored column, which is
        lexicographically ordered for a fixed offset. Pass UTC values.
        """
        lo = (
            start
            if isinstance(start, str)
            else start.astimezone(timezone.utc).isoformat()
        )
        hi = end if isinstance(end, str) else end.astimezone(timezone.utc).isoformat()
        sql = (
            self._SELECT
            + " WHERE f.timestamp >= ? AND f.timestamp < ? ORDER BY f.timestamp"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._rows_to_frames(self.conn.execute(sql, (lo, hi)))

    def frame(self, frame_id: int) -> Frame | None:
        rows = list(self.conn.execute(self._SELECT + " WHERE f.id = ?", (frame_id,)))
        found = self._rows_to_frames(rows)
        return found[0] if found else None

    def frames_by_id(self, frame_ids: list[int]) -> list[Frame]:
        if not frame_ids:
            return []
        marks = ",".join("?" * len(frame_ids))
        rows = self.conn.execute(
            self._SELECT + f" WHERE f.id IN ({marks}) ORDER BY f.timestamp", frame_ids
        )
        return self._rows_to_frames(rows)

    def neighbours(self, frame_id: int, before: int = 1, after: int = 1) -> list[Frame]:
        """Frames immediately around ``frame_id`` in capture order."""
        ids = list(range(frame_id - max(before, 0), frame_id + max(after, 0) + 1))
        return self.frames_by_id(ids)

    def grep(
        self,
        pattern: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[Frame]:
        """Frames whose text contains ``pattern`` (SQL LIKE, case-insensitive)."""
        sql = (
            self._SELECT + " WHERE (f.full_text LIKE ? OR f.accessibility_text LIKE ?)"
        )
        args: list = [f"%{pattern}%", f"%{pattern}%"]
        if start:
            sql += " AND f.timestamp >= ?"
            args.append(start)
        if end:
            sql += " AND f.timestamp < ?"
            args.append(end)
        sql += f" ORDER BY f.timestamp LIMIT {int(limit)}"
        return self._rows_to_frames(self.conn.execute(sql, args))

    def span(self) -> tuple[datetime, datetime]:
        row = self.conn.execute(
            "SELECT MIN(timestamp) a, MAX(timestamp) b FROM frames"
        ).fetchone()
        return parse_ts(row["a"]), parse_ts(row["b"])

    def count(self, start: str, end: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM frames WHERE timestamp >= ? AND timestamp < ?",
            (start, end),
        ).fetchone()
        return row["c"]

    # ----------------------------------------------------------------- audio

    def transcripts(self, start: str, end: str) -> list[dict]:
        """Audio transcription rows overlapping the window, if any were captured."""
        try:
            rows = self.conn.execute(
                """
                SELECT at.timestamp, at.transcription, at.device, at.is_input_device
                FROM audio_transcriptions at
                WHERE at.timestamp >= ? AND at.timestamp < ?
                  AND TRIM(COALESCE(at.transcription, '')) != ''
                ORDER BY at.timestamp
                """,
                (start, end),
            )
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- pixels

    def frame_png(self, frame_id: int, max_width: int = 1280) -> Path:
        """Extract one frame's pixels to a cached PNG and return its path.

        Selects by decoded frame index so the returned pixels are the same frame
        whose OCR is stored in the row.
        """
        frame = self.frame(frame_id)
        if frame is None:
            raise KeyError(f"frame {frame_id} not in archive")
        if not frame.chunk_path:
            raise ValueError(
                f"frame {frame_id} has no video chunk (snapshot-only capture?)"
            )
        chunk = Path(frame.chunk_path)
        if not chunk.exists():
            raise FileNotFoundError(f"chunk missing for frame {frame_id}: {chunk}")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / f"frame_{frame_id}_w{max_width}.png"
        if out.exists() and out.stat().st_size > 0:
            return out

        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(chunk),
            "-vf",
            f"select=eq(n\\,{frame.offset_index}),scale='min({max_width},iw)':-2",
            "-vsync",
            "0",
            "-frames:v",
            "1",
            "-y",
            str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no pixels for frame {frame_id}")
        return out


# --------------------------------------------------------------- text utils

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^\w\s:/.\-|]+")
_DIGITS = re.compile(r"\d+")


def normalize(text: str) -> str:
    """Collapse OCR noise so two renderings of the same screen compare equal."""
    text = _NOISE.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def tokens(text: str, drop_numbers: bool = True, min_len: int = 3) -> set[str]:
    """Content tokens of a frame's text.

    Numbers are dropped by default: on a game HUD or a dashboard almost every
    frame differs only in counters, and counting those as change makes every
    frame look novel.
    """
    norm = normalize(text)
    if drop_numbers:
        norm = _DIGITS.sub(" ", norm)
    return {t for t in norm.split() if len(t) >= min_len}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_fingerprint(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=4)
def open_archive(root: str = str(DEFAULT_ARCHIVE)) -> Archive:
    return Archive(root)


@dataclass
class Window:
    """A labelled time window, used for ground truth and for scoring."""

    start: datetime
    end: datetime
    label: str
    attributes: dict = field(default_factory=dict)

    def overlap(self, other: "Window") -> float:
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        inter = max(0.0, (hi - lo).total_seconds())
        union = (
            max(self.end, other.end) - min(self.start, other.start)
        ).total_seconds()
        return inter / union if union else 0.0
