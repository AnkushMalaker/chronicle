"""Representative images for timeline episodes, sampled from the node's frame store.

An episode's picture used to be whichever frame an *observation* happened to shortlist
while it was open. That couples the timeline's visual layer to a decision made minutes
earlier for a different purpose, and it shows: a 55-minute gaming session was
represented by its main menu, because the only image-bearing observations it cited were
from the session's first twenty minutes.

ScreenPipe keeps every frame locally — ~500 for a half-hour session — so an episode can
instead ask the node to sample *its own interval*. The frames come back stratified
across the episode, a vision pass picks the one that depicts it, and the rest are
dropped.
"""

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from advanced_omi_backend.models.device_input import DeviceInputJob
from advanced_omi_backend.models.timeline import TimelineDay, TimelineEpisode, utcnow
from advanced_omi_backend.services.memory.agent.codex_agent import (
    codex_executor_available,
)
from advanced_omi_backend.services.observation_curation import (
    _observation_codex_settings,
)

logger = logging.getLogger(__name__)

# One request covers a whole episode, so this is a per-episode cost, not per-frame.
FRAMES_PER_EPISODE = 6
# Wide enough for the picker to read a scoreboard or a document title.
FRAME_WIDTH = 960
# Episodes handled per cron tick. Each is one node round trip plus one vision call.
_EPISODE_BATCH = 12

_CHOICE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_frame_id": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["selected_frame_id", "reason"],
}

_PROMPT = """\
Choose the single frame that best depicts this timeline episode for someone scanning
their day. The images are named `frame-<id>.jpg` and are sampled evenly across the
episode's span, so they are moments from it, not a sequence to describe.

Prefer a frame showing the activity actually happening — play in progress, the document
being written, the call in session — over menus, loading screens, launchers, empty
desktops, and idle or blank screens. Prefer legible content over an exact moment;
"good enough" is the bar. Set `selected_frame_id` to null only when every frame is
blank, locked, or otherwise depicts nothing.
"""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _request_frames(episode: TimelineEpisode, source_id: str) -> None:
    """Ask the node for frames spread across this episode's interval."""

    await DeviceInputJob(
        user_id=episode.user_id,
        source_id=source_id,
        kind="thumbnail",
        start_at=_utc(episode.started_at),
        end_at=_utc(episode.ended_at),
        purpose="episode_frames",
        payload={
            "episode_id": episode.episode_id,
            "count": FRAMES_PER_EPISODE,
            "width": FRAME_WIDTH,
        },
    ).insert()
    episode.thumbnail_state = "requested"
    await episode.save()


async def choose_episode_frame(episode: TimelineEpisode) -> dict[str, Any]:
    """Run the vision pass over an episode's fetched frames."""

    settings = _observation_codex_settings()
    available, detail = codex_executor_available()
    if not available:
        raise RuntimeError(detail)
    context = {
        "title": episode.title,
        "summary": episode.summary,
        "kind": episode.kind,
        "entities": episode.entities,
        "started_at": _utc(episode.started_at).isoformat(),
        "ended_at": _utc(episode.ended_at).isoformat(),
        "frame_ids": [frame["frame_id"] for frame in episode.frame_shortlist],
    }
    with tempfile.TemporaryDirectory(prefix="chronicle-episode-frame-") as temp_dir:
        workspace = Path(temp_dir)
        schema_path = workspace / "choice-schema.json"
        output_path = workspace / "choice.json"
        schema_path.write_text(json.dumps(_CHOICE_SCHEMA), encoding="utf-8")
        command = [
            detail,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-m",
            settings["model"],
        ]
        if settings["reasoning_effort"]:
            command += [
                "-c",
                f'model_reasoning_effort="{settings["reasoning_effort"]}"',
            ]
        for frame in episode.frame_shortlist:
            image_path = workspace / f"frame-{frame['frame_id']}.jpg"
            image_path.write_bytes(frame["data"])
            command += ["--image", str(image_path)]
        command.append("-")
        prompt = f"{_PROMPT}\n\nEpisode:\n{json.dumps(context, ensure_ascii=False)}"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")),
            timeout=settings["timeout_seconds"],
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"episode frame choice failed: {stderr.decode(errors='replace')[-2000:]}"
            )
        return json.loads(output_path.read_text(encoding="utf-8"))


def apply_frame_choice(episode: TimelineEpisode, choice: dict[str, Any]) -> bool:
    """Keep the chosen frame and drop the shortlist. False when none was usable."""

    frame_id = choice.get("selected_frame_id")
    chosen = next(
        (
            frame
            for frame in episode.frame_shortlist
            if frame_id is not None and frame["frame_id"] == int(frame_id)
        ),
        None,
    )
    episode.frame_shortlist = []
    if chosen is None:
        episode.thumbnail_state = "unavailable"
        return False
    episode.representative_image = chosen["data"]
    episode.representative_image_type = chosen.get("content_type") or "image/jpeg"
    episode.thumbnail_state = "chosen"
    episode.revised_at = utcnow()
    return True


async def process_episode_thumbnails() -> dict[str, int]:
    """Give episodes on active runs a picture drawn from their own interval.

    Two phases, so a tick never blocks on the node: episodes with no frames yet get a
    request, and episodes whose frames have arrived get the vision pass.
    """

    days = await TimelineDay.find(
        TimelineDay.active_run_id != None
    ).to_list()  # noqa: E711
    run_ids = [day.active_run_id for day in days if day.active_run_id]
    if not run_ids:
        return {"requested": 0, "chosen": 0, "unavailable": 0}
    episodes = (
        await TimelineEpisode.find(
            {
                "run_id": {"$in": run_ids},
                # "not terminal", not an enumeration of the live states: an episode
                # written before this field existed has no `thumbnail_state` at all,
                # and `$in` skips a missing field while `$nin` matches it. Beanie
                # supplies the "" default on read, so the model hides the difference.
                "thumbnail_state": {"$nin": ["chosen", "unavailable"]},
            }
        )
        .sort("-started_at")
        .limit(_EPISODE_BATCH)
        .to_list()
    )
    requested = chosen = unavailable = 0
    for episode in episodes:
        if not episode.frame_shortlist:
            if episode.thumbnail_state == "requested":
                continue
            source_id = next(iter(episode.source_ids), None)
            if source_id is None:
                episode.thumbnail_state = "unavailable"
                await episode.save()
                unavailable += 1
                continue
            await _request_frames(episode, source_id)
            requested += 1
            continue
        try:
            choice = await choose_episode_frame(episode)
        except Exception as exc:
            logger.warning(
                "episode %s frame choice failed: %s", episode.episode_id, exc
            )
            continue
        if apply_frame_choice(episode, choice):
            chosen += 1
        else:
            unavailable += 1
        await episode.save()
    return {"requested": requested, "chosen": chosen, "unavailable": unavailable}
