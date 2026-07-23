"""Event-driven ScreenPipe observation state machine.

The collector polls ScreenPipe's local database frequently, but this module emits
remote events only for meaningful context boundaries and novel samples.  State is
plain JSON so an open observation survives collector restarts.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

_MEANINGFUL_TRIGGERS = {
    "click",
    "typing_pause",
    "scroll_stop",
    "key_press",
    "clipboard",
    "visual_change",
    "manual",
}
_INACTIVE_TRIGGERS = {"idle", "locked", "blank", "drm_paused"}
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def iso_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value)
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return f"{text}Z"


def timestamp_seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def normalize_text(value: str | None, limit: int = 2000) -> str:
    """Normalize and bound locally captured text before it leaves the device."""
    return " ".join((value or "").split())[:limit]


def content_fingerprint(value: str | None) -> str:
    normalized = normalize_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def text_is_novel(previous: str, current: str) -> bool:
    """Reject exact and near-identical OCR/accessibility snapshots."""
    previous = normalize_text(previous)
    current = normalize_text(current)
    if not current or previous == current:
        return False
    if not previous:
        return True
    if SequenceMatcher(None, previous, current).ratio() >= 0.92:
        return False
    old_tokens = set(_TOKEN_RE.findall(previous.lower()))
    new_tokens = set(_TOKEN_RE.findall(current.lower()))
    if old_tokens or new_tokens:
        union = old_tokens | new_tokens
        if union and len(old_tokens & new_tokens) / len(union) >= 0.88:
            return False
    return True


def context_key(row: Mapping[str, Any]) -> list[str]:
    return [
        str(row["app_name"] or ""),
        str(row["window_name"] or ""),
        str(_row_value(row, "browser_url") or ""),
    ]


def _row_value(row: Mapping[str, Any], name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _sample(
    observation: dict[str, Any], trigger: str, *, liveness: bool = False
) -> dict[str, Any]:
    text = observation.get("text", "")
    return {
        "captured_at": observation["ended_at"],
        "elapsed_seconds": max(
            0,
            timestamp_seconds(observation["ended_at"])
            - timestamp_seconds(observation["captured_at"]),
        ),
        "capture_trigger": trigger,
        "text": text,
        "content_fingerprint": content_fingerprint(text),
        "previous_fingerprint": observation.get("last_sent_fingerprint"),
        "frame_id": observation["representative_frame_id"],
        "liveness": liveness,
        "inactive": trigger in _INACTIVE_TRIGGERS,
    }


def _candidate_score(row: Mapping[str, Any], text: str, frame_count: int) -> float:
    trigger = str(_row_value(row, "capture_trigger") or "")
    return (
        min(len(text), 1000) / 1000
        + (0.75 if trigger in _MEANINGFUL_TRIGGERS else 0)
        + (0.25 if frame_count > 1 else 0)
        - (1.0 if trigger in _INACTIVE_TRIGGERS else 0)
    )


def _add_frame_candidate(
    observation: dict[str, Any], row: Mapping[str, Any], text: str
) -> None:
    frame_id = int(row["id"])
    candidates = [
        candidate
        for candidate in observation.get("frame_candidates", [])
        if candidate["frame_id"] != frame_id
    ]
    candidates.append(
        {
            "frame_id": frame_id,
            "captured_at": iso_timestamp(row["timestamp"]),
            "score": round(_candidate_score(row, text, observation["frame_count"]), 4),
            "capture_trigger": str(_row_value(row, "capture_trigger") or ""),
            "has_text": bool(text),
        }
    )
    observation["frame_candidates"] = sorted(
        candidates, key=lambda candidate: (-candidate["score"], candidate["frame_id"])
    )[:3]


def new_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    captured_at = iso_timestamp(row["timestamp"])
    text = normalize_text(_row_value(row, "full_text"))
    trigger = str(_row_value(row, "capture_trigger") or "")
    observation = {
        "key": context_key(row),
        "source_item_id": f"observation:{row['id']}",
        "captured_at": captured_at,
        "ended_at": captured_at,
        "first_frame_id": int(row["id"]),
        "last_frame_id": int(row["id"]),
        "representative_frame_id": int(row["id"]),
        "frame_count": 1,
        "app_name": context_key(row)[0],
        "window_name": context_key(row)[1],
        "browser_url": context_key(row)[2],
        "capture_trigger": trigger,
        "text": text,
        "initial_text": text,
        "last_sent_text": "",
        "last_sent_fingerprint": None,
        "last_sample_at": None,
        "meaningful": trigger in _MEANINGFUL_TRIGGERS,
        "inactive": trigger in _INACTIVE_TRIGGERS,
        "frame_candidates": [],
    }
    _add_frame_candidate(observation, row, text)
    return observation


def update_observation(observation: dict[str, Any], row: Mapping[str, Any]) -> None:
    text = normalize_text(_row_value(row, "full_text"))
    trigger = str(_row_value(row, "capture_trigger") or "")
    observation["ended_at"] = iso_timestamp(row["timestamp"])
    observation["last_frame_id"] = int(row["id"])
    observation["frame_count"] += 1
    observation["capture_trigger"] = trigger or observation.get("capture_trigger", "")
    observation["inactive"] = trigger in _INACTIVE_TRIGGERS
    if trigger in _MEANINGFUL_TRIGGERS or text_is_novel(
        observation["initial_text"], text
    ):
        observation["meaningful"] = True
    if text:
        observation["text"] = text
        observation["representative_frame_id"] = int(row["id"])
    _add_frame_candidate(observation, row, text)


def _metadata(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: observation.get(key)
        for key in (
            "app_name",
            "window_name",
            "browser_url",
            "first_frame_id",
            "last_frame_id",
            "representative_frame_id",
            "frame_count",
            "capture_trigger",
            "inactive",
        )
    }


def _event(
    action: str,
    observation: dict[str, Any],
    *,
    ended_at: str | None = None,
    sample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event": action,
        "source_item_id": observation["source_item_id"],
        "captured_at": observation["captured_at"],
        "ended_at": ended_at,
        "metadata": _metadata(observation),
        "frame_candidates": observation.get("frame_candidates", []),
        "sample": sample,
    }


class ObservationTracker:
    """Track one committed observation and one uncommitted switch candidate."""

    SCHEMA = 1

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        stability_seconds: float = 10,
        sample_cooldown_seconds: float = 120,
        liveness_seconds: float = 900,
    ):
        state = state or {"schema": self.SCHEMA, "active": None, "candidate": None}
        if state.get("schema") != self.SCHEMA:
            raise ValueError("unsupported observation state schema")
        self.state = state
        self.stability_seconds = stability_seconds
        self.sample_cooldown_seconds = sample_cooldown_seconds
        self.liveness_seconds = liveness_seconds

    @property
    def active(self) -> dict[str, Any] | None:
        return self.state["active"]

    @active.setter
    def active(self, value: dict[str, Any] | None) -> None:
        self.state["active"] = value

    @property
    def candidate(self) -> dict[str, Any] | None:
        return self.state["candidate"]

    @candidate.setter
    def candidate(self, value: dict[str, Any] | None) -> None:
        self.state["candidate"] = value

    def _open_candidate(self) -> list[dict[str, Any]]:
        if self.candidate is None:
            return []
        events: list[dict[str, Any]] = []
        if self.active is not None:
            self.active["ended_at"] = self.candidate["captured_at"]
            events.append(
                _event("close", self.active, ended_at=self.active["ended_at"])
            )
        opened = self.candidate
        opened["opened_early"] = bool(opened.get("meaningful")) and (
            timestamp_seconds(opened["ended_at"])
            - timestamp_seconds(opened["captured_at"])
            < self.stability_seconds
        )
        initial_sample = _sample(opened, opened.get("capture_trigger", ""))
        opened["last_sent_text"] = opened.get("text", "")
        opened["last_sent_fingerprint"] = initial_sample["content_fingerprint"]
        opened["last_sample_at"] = initial_sample["captured_at"]
        events.append(_event("open", opened, sample=initial_sample))
        self.active = opened
        self.candidate = None
        return events

    def _close_active(self, ended_at: str) -> list[dict[str, Any]]:
        if self.active is None:
            return []
        self.active["ended_at"] = ended_at
        final_sample = None
        if text_is_novel(
            self.active.get("last_sent_text", ""), self.active.get("text", "")
        ):
            final_sample = _sample(self.active, self.active.get("capture_trigger", ""))
            self.active["last_sent_text"] = self.active.get("text", "")
            self.active["last_sent_fingerprint"] = final_sample["content_fingerprint"]
            self.active["last_sample_at"] = final_sample["captured_at"]
        events = [_event("close", self.active, ended_at=ended_at, sample=final_sample)]
        self.active = None
        return events

    def _maybe_sample(self) -> list[dict[str, Any]]:
        observation = self.active
        if observation is None or not observation.get("last_sample_at"):
            return []
        elapsed = timestamp_seconds(observation["ended_at"]) - timestamp_seconds(
            observation["last_sample_at"]
        )
        novel = text_is_novel(
            observation.get("last_sent_text", ""), observation.get("text", "")
        )
        liveness = elapsed >= self.liveness_seconds and not observation.get("inactive")
        if not liveness and (not novel or elapsed < self.sample_cooldown_seconds):
            return []
        sample = _sample(
            observation,
            observation.get("capture_trigger", ""),
            liveness=liveness and not novel,
        )
        observation["last_sent_text"] = observation.get("text", "")
        observation["last_sent_fingerprint"] = sample["content_fingerprint"]
        observation["last_sample_at"] = sample["captured_at"]
        return [_event("sample", observation, sample=sample)]

    def _candidate_is_stable(self, now: str) -> bool:
        return self.candidate is not None and (
            timestamp_seconds(now) - timestamp_seconds(self.candidate["captured_at"])
            >= self.stability_seconds
        )

    def process_rows(
        self, rows: Iterable[Mapping[str, Any]], now: str
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in rows:
            key = context_key(row)
            captured_at = iso_timestamp(row["timestamp"])
            if self.candidate is not None:
                if self.candidate["key"] == key:
                    update_observation(self.candidate, row)
                    if self.candidate["meaningful"] or self._candidate_is_stable(
                        captured_at
                    ):
                        events.extend(self._open_candidate())
                    continue

                if self.active is not None and self.active["key"] == key:
                    if self.candidate["meaningful"]:
                        events.extend(self._open_candidate())
                        events.extend(self._close_active(captured_at))
                        self.candidate = new_observation(row)
                    else:
                        self.candidate = None
                        update_observation(self.active, row)
                        events.extend(self._maybe_sample())
                    continue

                if self.candidate["meaningful"]:
                    events.extend(self._open_candidate())
                    events.extend(self._close_active(captured_at))
                self.candidate = new_observation(row)
                continue

            if self.active is None:
                self.candidate = new_observation(row)
                if self.candidate["meaningful"]:
                    events.extend(self._open_candidate())
                continue
            if self.active["key"] == key:
                update_observation(self.active, row)
                events.extend(self._maybe_sample())
                continue
            if self.active.get("opened_early"):
                events.extend(self._close_active(captured_at))
            self.candidate = new_observation(row)

        if self._candidate_is_stable(now):
            events.extend(self._open_candidate())
        return events

    def close(self, now: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self.candidate is not None and self.candidate.get("meaningful"):
            events.extend(self._open_candidate())
        self.candidate = None
        events.extend(self._close_active(now))
        return events
