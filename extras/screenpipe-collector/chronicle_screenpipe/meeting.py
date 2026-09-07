"""Meeting detection for the ScreenPipe collector.

Follows the pattern production note-takers (Granola, ScreenPipe's own meeting
watcher) converged on: a process holding a *running* microphone capture stream
is the trigger, app identity is the classifier, and a confirm/grace state
machine provides hysteresis. Detection is allowlist-only — a known meeting app
classifies directly, a browser is attributed through the current observation's
``browser_url`` — so always-on capturers (ScreenPipe itself, OBS, dictation
tools) never mint meetings.

The sensor reads the PipeWire graph via ``pw-dump``; hosts without PipeWire
simply report no sensor and the tracker stays idle. State is plain JSON so an
open meeting survives collector restarts, and closed intervals are retained so
audio chunks collected minutes later can still be tagged by timestamp overlap.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .observations import _row_value, iso_timestamp, timestamp_seconds

logger = logging.getLogger(__name__)

# Native applications whose running mic capture is itself a call. Matched as a
# substring of the process binary or application name, lowercased.
_MEETING_APP_PLATFORMS = {
    "zoom": "zoom",
    "teams": "teams",
    "webex": "webex",
    "discord": "discord",
    "slack": "slack",
    "skype": "skype",
    "jitsi": "jitsi",
    "telegram": "telegram",
    "signal": "signal",
}

# Browsers hold the mic for many reasons in principle, but Chromium and Firefox
# only keep a capture stream *running* while a page holds a live getUserMedia
# track, so in practice a running browser capture is a call. The current
# observation's URL refines the platform when it can.
_BROWSER_BINARIES = (
    "firefox",
    "librewolf",
    "zen",
    "chrome",
    "chromium",
    "brave",
    "vivaldi",
    "edge",
    "opera",
)

_URL_PLATFORMS = (
    ("meet.google.com", "google-meet"),
    ("zoom.us", "zoom"),
    ("teams.microsoft.com", "teams"),
    ("teams.live.com", "teams"),
    ("webex.com", "webex"),
    ("whereby.com", "whereby"),
    ("meet.jit.si", "jitsi"),
    ("discord.com", "discord"),
    ("gather.town", "gather"),
    ("around.co", "around"),
)


@dataclass(frozen=True)
class CaptureApp:
    """A process currently holding a running audio-capture stream."""

    name: str
    binary: str


def pipewire_capture_apps(timeout: float = 5.0) -> list[CaptureApp] | None:
    """Snapshot processes with a running mic capture stream, or None if the
    sensor is unavailable (no PipeWire, pw-dump missing, or a bad dump)."""
    try:
        completed = subprocess.run(
            ["pw-dump"], capture_output=True, timeout=timeout, check=True
        )
        graph = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(graph, list):
        return None
    apps = []
    for obj in graph:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        if info.get("state") != "running":
            continue
        apps.append(
            CaptureApp(
                name=str(props.get("application.name") or ""),
                binary=str(
                    props.get("application.process.binary")
                    or props.get("node.name")
                    or ""
                ),
            )
        )
    return apps


def classify_capture(
    apps: list[CaptureApp], context: Mapping[str, Any] | None
) -> dict[str, str] | None:
    """Return {platform, app, browser_url} for the first meeting-classified
    capture, or None. ``context`` is the collector's current observation
    (app_name / window_name / browser_url), used to attribute browser mics."""
    browser_url = str((context or {}).get("browser_url") or "")
    url = browser_url.lower()
    for app in apps:
        identity = f"{app.binary} {app.name}".lower()
        if not identity.strip():
            continue
        for needle, platform in _MEETING_APP_PLATFORMS.items():
            if needle in identity:
                return {"platform": platform, "app": app.binary or app.name}
        if any(browser in identity for browser in _BROWSER_BINARIES):
            for needle, platform in _URL_PLATFORMS:
                if needle in url:
                    return {
                        "platform": platform,
                        "app": app.binary or app.name,
                        "browser_url": browser_url,
                    }
            return {"platform": "browser-call", "app": app.binary or app.name}
    return None


def _meeting_event(
    action: str,
    meeting: dict[str, Any],
    *,
    capture_source_id: str,
    ended_at: str | None = None,
) -> dict[str, Any]:
    """Shape a meeting boundary as a standard observation event."""
    locator = {
        "capture_source_id": capture_source_id,
        "modality": "context",
        "track_id": "meeting-detector",
    }
    metadata = {
        "observation_type": "meeting",
        "platform": meeting["platform"],
        "app_name": meeting.get("app", ""),
        "browser_url": meeting.get("browser_url", ""),
        "locator": locator,
    }
    if meeting.get("title"):
        metadata["title"] = meeting["title"]
    if meeting.get("source"):
        metadata["detection_source"] = meeting["source"]
    return {
        "event": action,
        "source_item_id": meeting["meeting_id"],
        "locator": locator,
        "captured_at": meeting["started_at"],
        "ended_at": ended_at,
        "metadata": metadata,
        "frame_candidates": [],
        "sample": None,
    }


def platform_from_app(app: str) -> str:
    """Map the recorder's display name ("Google Meet", "Zoom") to a slug."""
    value = app.lower()
    for needle, platform in _MEETING_APP_PLATFORMS.items():
        if needle in value:
            return platform
    if "meet" in value:
        return "google-meet"
    slug = "-".join(value.split())
    return slug or "meeting"


class RecorderMeetingLog:
    """Mirror ScreenPipe's own ``meetings`` table into Chronicle events.

    On macOS and Windows the recorder's meeting watcher persists meetings in
    its database — a row appears when a meeting opens and gains
    ``meeting_end`` when it closes. Rows are diffed against sent-state so each
    boundary is emitted exactly once, *including retroactively* for meetings
    recorded while the collector was down. Where the recorder writes meetings,
    it owns detection and the PipeWire tracker stands down.
    """

    SCHEMA = 1

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        capture_source_id: str = "screenpipe",
        retain_seconds: float = 48 * 3600.0,
        owns_detection_seconds: float = 7 * 24 * 3600.0,
    ):
        state = state or {"schema": self.SCHEMA, "rows": {}}
        if state.get("schema") != self.SCHEMA:
            raise ValueError("unsupported recorder meeting state schema")
        self.state = state
        self.capture_source_id = capture_source_id
        self.retain_seconds = retain_seconds
        self.owns_detection_seconds = owns_detection_seconds

    def sync(self, rows: Iterable[Mapping[str, Any]], now: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in rows:
            row_id = str(row["id"])
            app = str(row["meeting_app"] or "")
            ended_at = iso_timestamp(row["meeting_end"]) if row["meeting_end"] else None
            meeting = {
                "meeting_id": f"meeting:recorder:{row_id}",
                "started_at": iso_timestamp(row["meeting_start"]),
                "ended_at": ended_at,
                "platform": platform_from_app(app),
                "app": app,
                "title": str(_row_value(row, "title") or ""),
                "source": "recorder",
            }
            known = self.state["rows"].get(row_id)
            if known is None:
                self.state["rows"][row_id] = meeting
                events.append(
                    _meeting_event(
                        "open", meeting, capture_source_id=self.capture_source_id
                    )
                )
                if ended_at:
                    meeting["close_sent"] = True
                    events.append(
                        _meeting_event(
                            "close",
                            meeting,
                            capture_source_id=self.capture_source_id,
                            ended_at=ended_at,
                        )
                    )
                continue
            known.update({"ended_at": ended_at, "title": meeting["title"], "app": app})
            if ended_at and not known.get("close_sent"):
                known["close_sent"] = True
                events.append(
                    _meeting_event(
                        "close",
                        known,
                        capture_source_id=self.capture_source_id,
                        ended_at=ended_at,
                    )
                )
        horizon = timestamp_seconds(iso_timestamp(now)) - self.retain_seconds
        self.state["rows"] = {
            row_id: row
            for row_id, row in self.state["rows"].items()
            if row["ended_at"] is None
            or not row.get("close_sent")
            or timestamp_seconds(row["ended_at"]) >= horizon
        }
        return events

    def owns_detection(self, now: str) -> bool:
        """The recorder detected a meeting recently, so it owns this node."""
        horizon = timestamp_seconds(iso_timestamp(now)) - self.owns_detection_seconds
        return any(
            timestamp_seconds(row["started_at"]) >= horizon
            for row in self.state["rows"].values()
        )

    def active_platform(self) -> str | None:
        for row in self.state["rows"].values():
            if row["ended_at"] is None:
                return row["platform"]
        return None

    def meeting_for(self, start: str, end: str) -> str | None:
        chunk_start = timestamp_seconds(iso_timestamp(start))
        chunk_end = timestamp_seconds(iso_timestamp(end))
        for row in self.state["rows"].values():
            still_open = row["ended_at"] is None or chunk_start <= timestamp_seconds(
                row["ended_at"]
            )
            if still_open and chunk_end >= timestamp_seconds(row["started_at"]):
                return row["meeting_id"]
        return None


class MeetingTracker:
    """Idle → confirming → active → ending state machine over sensor snapshots.

    A sighting must repeat ``confirm_polls`` times before a meeting opens
    (pre-join lobbies, mic tests), and the capture stream must stay gone for
    ``grace_seconds`` before it closes (device switches, brief drops). A
    sensor failure (snapshot ``None``) changes nothing until it has persisted
    ``stale_seconds`` past the last positive sighting, at which point an active
    meeting is closed at that last sighting rather than left open forever.
    """

    SCHEMA = 1

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        capture_source_id: str = "screenpipe",
        confirm_polls: int = 2,
        grace_seconds: float = 30.0,
        stale_seconds: float = 600.0,
        retain_seconds: float = 48 * 3600.0,
    ):
        state = state or {
            "schema": self.SCHEMA,
            "phase": "idle",
            "meeting": None,
            "confirm_count": 0,
            "ending_since": None,
            "recent": [],
        }
        if state.get("schema") != self.SCHEMA:
            raise ValueError("unsupported meeting state schema")
        self.state = state
        self.capture_source_id = capture_source_id
        self.confirm_polls = confirm_polls
        self.grace_seconds = grace_seconds
        self.stale_seconds = stale_seconds
        self.retain_seconds = retain_seconds

    @property
    def meeting(self) -> dict[str, Any] | None:
        return self.state["meeting"]

    def active_platform(self) -> str | None:
        if self.state["phase"] in {"active", "ending"} and self.meeting:
            return self.meeting["platform"]
        return None

    def _close_meeting(self, ended_at: str) -> list[dict[str, Any]]:
        meeting = self.meeting
        assert meeting is not None
        events = [
            _meeting_event(
                "close",
                meeting,
                capture_source_id=self.capture_source_id,
                ended_at=ended_at,
            )
        ]
        self.state["recent"].append(
            {
                "meeting_id": meeting["meeting_id"],
                "platform": meeting["platform"],
                "started_at": meeting["started_at"],
                "ended_at": ended_at,
            }
        )
        horizon = timestamp_seconds(ended_at) - self.retain_seconds
        self.state["recent"] = [
            row
            for row in self.state["recent"]
            if timestamp_seconds(row["ended_at"]) >= horizon
        ]
        self.state.update(
            {"phase": "idle", "meeting": None, "confirm_count": 0, "ending_since": None}
        )
        return events

    def tick(
        self,
        apps: list[CaptureApp] | None,
        context: Mapping[str, Any] | None,
        now: str,
    ) -> list[dict[str, Any]]:
        now = iso_timestamp(now)
        phase = self.state["phase"]
        if apps is None:
            # Sensor failure: hold state unless it has been failing so long
            # that an open meeting would otherwise never close.
            meeting = self.meeting
            if (
                phase in {"active", "ending"}
                and meeting is not None
                and timestamp_seconds(now) - timestamp_seconds(meeting["last_seen_at"])
                >= self.stale_seconds
            ):
                return self._close_meeting(meeting["last_seen_at"])
            return []

        detection = classify_capture(apps, context)
        if detection is not None:
            if phase == "idle":
                self.state["phase"] = "confirming"
                self.state["confirm_count"] = 1
                self.state["meeting"] = {
                    "meeting_id": f"meeting:{int(timestamp_seconds(now))}",
                    "started_at": now,
                    "last_seen_at": now,
                    "source": "pipewire",
                    **detection,
                }
                return []
            meeting = self.meeting
            assert meeting is not None
            meeting["last_seen_at"] = now
            # A URL-attributed platform beats the generic browser fallback,
            # whenever the tab becomes visible.
            if meeting["platform"] == "browser-call" != detection["platform"]:
                meeting.update(detection)
            if phase == "confirming":
                self.state["confirm_count"] += 1
                if self.state["confirm_count"] >= self.confirm_polls:
                    self.state["phase"] = "active"
                    return [
                        _meeting_event(
                            "open",
                            meeting,
                            capture_source_id=self.capture_source_id,
                        )
                    ]
                return []
            if phase == "ending":
                self.state["phase"] = "active"
                self.state["ending_since"] = None
            return []

        if phase == "confirming":
            self.state.update({"phase": "idle", "meeting": None, "confirm_count": 0})
            return []
        if phase == "active":
            self.state["phase"] = "ending"
            self.state["ending_since"] = now
            return []
        if phase == "ending":
            ended = self.state["ending_since"]
            if timestamp_seconds(now) - timestamp_seconds(ended) >= self.grace_seconds:
                return self._close_meeting(ended)
        return []

    def retire(self) -> list[dict[str, Any]]:
        """Stand down because another detector owns this node.

        An open meeting is closed at its last confirmed sighting; a mere
        candidate is discarded. Recent closed intervals are kept so
        already-detected chunks still tag."""
        meeting = self.meeting
        if self.state["phase"] in {"active", "ending"} and meeting is not None:
            return self._close_meeting(
                self.state["ending_since"] or meeting["last_seen_at"]
            )
        self.state.update({"phase": "idle", "meeting": None, "confirm_count": 0})
        return []

    def meeting_for(self, start: str, end: str) -> str | None:
        """The meeting overlapping [start, end], if any — active or recent."""
        chunk_start = timestamp_seconds(iso_timestamp(start))
        chunk_end = timestamp_seconds(iso_timestamp(end))
        meeting = self.meeting
        if self.state["phase"] in {"active", "ending"} and meeting is not None:
            # An active meeting has no end yet; only bound it once it is ending.
            ending_since = self.state["ending_since"]
            still_open = ending_since is None or chunk_start <= timestamp_seconds(
                ending_since
            )
            if still_open and chunk_end >= timestamp_seconds(meeting["started_at"]):
                return meeting["meeting_id"]
        for row in reversed(self.state["recent"]):
            if chunk_start <= timestamp_seconds(
                row["ended_at"]
            ) and chunk_end >= timestamp_seconds(row["started_at"]):
                return row["meeting_id"]
        return None
