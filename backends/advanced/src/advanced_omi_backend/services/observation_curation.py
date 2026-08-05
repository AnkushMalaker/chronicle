"""Conservative Codex curation for event-driven screen observations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    DeviceInputItem,
    DeviceInputJob,
    utcnow,
)
from advanced_omi_backend.services.memory.agent.codex_agent import (
    codex_executor_available,
)
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager
from advanced_omi_backend.services.memory.vault_media import promote_image_bytes

logger = logging.getLogger(__name__)

_OPEN_CURATION_INTERVAL = timedelta(minutes=15)
_AUDIO_LOOKBACK = timedelta(minutes=35)
_IMMICH_MARGIN = timedelta(minutes=30)
_CODEX_TIMEOUT_SECONDS = 600

_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": [
                "discard",
                "duplicate",
                "text_update",
                "dedicated_note",
                "promote_image",
                "alternative_preview",
            ],
        },
        "reason": {"type": "string"},
        "duplicate_observation_id": {"type": ["string", "null"]},
        "alternative_frame_id": {"type": ["integer", "null"]},
        "note_path": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "retain_image": {"type": "boolean"},
        "immich_item_id": {"type": ["string", "null"]},
    },
    "required": [
        "decision",
        "reason",
        "duplicate_observation_id",
        "alternative_frame_id",
        "note_path",
        "title",
        "summary",
        "facts",
        "retain_image",
        "immich_item_id",
    ],
}

_PROMPT = """\
You curate one Chronicle screen observation into a personal Obsidian vault. Return only
the requested structured decision. Be highly selective: routine navigation, repeated
screens, passive media, and low-information context should normally be discarded.

The screenshot is sparse supporting evidence, not permission to invent. `output` audio
is system/media audio: character dialogue, lyrics, presenters, and game dialogue are
NEVER facts about the user. You may retain a media title/episode/progress and an explicit
user reaction. `input` audio may be personal speech, but attribute facts only when the
transcript's speaker evidence supports it.

Use `text_update` for a small Daily/YYYY-MM-DD.md entry. Use `dedicated_note` only for a
durable event/project/topic/place/media experience; choose a safe relative `.md` path.
Use `promote_image` only when the ScreenPipe preview or one of the supplied Immich
thumbnail candidates adds durable value; set `immich_item_id` only for the latter. Use
`alternative_preview` only if this image is unusable and a genuinely different ranked
frame exists. Use `duplicate` only when the supplied canonical observation id is clear.
Never write a fake conversation note for screen context.
"""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def observation_revision(item: DeviceInputItem) -> str:
    payload = {
        "lifecycle": item.lifecycle,
        "related_conversation_ids": sorted(item.related_conversation_ids),
        "samples": [
            [sample.get("captured_at"), sample.get("content_fingerprint")]
            for sample in item.samples
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def safe_note_path(root: Path, requested: str | None, captured_at: datetime) -> Path:
    relative = requested or f"Daily/{_as_utc(captured_at).date().isoformat()}.md"
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.suffix.lower() != ".md":
        raise ValueError("curation note path must be a relative Markdown path")
    if candidate.parts and candidate.parts[0] in {
        "Templates",
        "Conversations",
        "_media",
    }:
        raise ValueError("screen observations cannot write to this vault area")
    resolved = (root / candidate).resolve()
    resolved.relative_to(root.resolve())
    return resolved


async def _related_context(
    item: DeviceInputItem,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    start = _as_utc(item.captured_at)
    end = _as_utc(item.ended_at or utcnow())
    conversations = await Conversation.find(
        Conversation.user_id == item.user_id,
        Conversation.created_at >= start - _AUDIO_LOOKBACK,
        Conversation.created_at <= end,
    ).to_list()
    audio: list[dict[str, Any]] = []
    related_ids: list[str] = []
    for conversation in conversations:
        conversation_start = _as_utc(conversation.created_at)
        conversation_end = conversation_start + timedelta(
            seconds=conversation.audio_total_duration or 0
        )
        if conversation_end < start or conversation_start > end:
            continue
        direction = "unknown"
        source_id = conversation.external_source_id or ""
        parts = source_id.split(":")
        if len(parts) >= 3 and parts[0] == "screenpipe":
            direction = parts[2]
        audio.append(
            {
                "conversation_id": conversation.conversation_id,
                "direction": direction,
                "summary": conversation.summary or conversation.detailed_summary,
                "transcript": (conversation.transcript or "")[:6000],
            }
        )
        related_ids.append(conversation.conversation_id)
    item.related_conversation_ids = sorted(set(related_ids))

    immich = await DeviceInputItem.find(
        DeviceInputItem.user_id == item.user_id,
        DeviceInputItem.kind == "immich_memory",
        DeviceInputItem.captured_at >= start - _IMMICH_MARGIN,
        DeviceInputItem.captured_at <= end + _IMMICH_MARGIN,
    ).to_list()
    immich_context = [
        {
            "id": str(candidate.id),
            "captured_at": candidate.captured_at.isoformat(),
            "metadata": candidate.metadata,
        }
        for candidate in immich[:12]
    ]
    duplicate_rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == item.user_id,
            DeviceInputItem.source_id == item.source_id,
            DeviceInputItem.kind == "observation",
            DeviceInputItem.captured_at >= start - timedelta(days=1),
            DeviceInputItem.captured_at < start,
        )
        .sort("-captured_at")
        .limit(20)
        .to_list()
    )
    duplicates = [
        {
            "id": str(candidate.id),
            "captured_at": candidate.captured_at.isoformat(),
            "metadata": candidate.metadata,
            "latest_text": (
                candidate.samples[-1].get("text", "") if candidate.samples else ""
            ),
            "curation": candidate.curation,
        }
        for candidate in duplicate_rows
    ]
    return audio, immich_context, duplicates


async def run_codex_observation_agent(
    item: DeviceInputItem,
    root: Path,
    audio: list[dict[str, Any]],
    immich: list[dict[str, Any]],
    duplicate_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    available, detail = codex_executor_available()
    if not available:
        raise RuntimeError(detail)
    binary = detail
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "observation_id": str(item.id),
        "source_item_id": item.source_item_id,
        "captured_at": item.captured_at.isoformat(),
        "ended_at": item.ended_at.isoformat() if item.ended_at else None,
        "lifecycle": item.lifecycle,
        "metadata": item.metadata,
        "samples": item.samples,
        "frame_candidates": item.frame_candidates,
        "related_audio": audio,
        "nearby_immich": immich,
        "duplicate_candidates": duplicate_candidates,
        "existing_vault_paths": ConvDocVaultManager().list_docs(item.user_id)[:200],
    }
    prompt = (
        f"{_PROMPT}\n\nObservation data:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    with tempfile.TemporaryDirectory(prefix="chronicle-observation-") as temp_dir:
        workspace = Path(temp_dir)
        schema_path = workspace / "decision-schema.json"
        output_path = workspace / "decision.json"
        schema_path.write_text(json.dumps(_DECISION_SCHEMA), encoding="utf-8")
        command = [
            binary,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(root),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if item.media_data:
            suffix = Path(item.media_filename or "preview.jpg").suffix or ".jpg"
            image_path = workspace / f"preview{suffix}"
            image_path.write_bytes(item.media_data)
            command.extend(["--image", str(image_path)])
        for index, candidate in enumerate(immich[:3]):
            asset_id = candidate.get("metadata", {}).get("asset_id")
            if not asset_id:
                continue
            try:
                data, content_type = await _immich_image(
                    str(asset_id), "thumbnail?size=thumbnail", max_bytes=5 * 1024 * 1024
                )
            except Exception as exc:
                logger.warning("Immich thumbnail %s unavailable: %s", asset_id, exc)
                continue
            suffix = ".png" if content_type == "image/png" else ".jpg"
            image_path = workspace / f"immich-{index}-{asset_id}{suffix}"
            image_path.write_bytes(data)
            command.extend(["--image", str(image_path)])
        command.append("-")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")), timeout=_CODEX_TIMEOUT_SECONDS
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"Codex observation curation failed: {stderr.decode(errors='replace')[-2000:]}"
            )
        return json.loads(output_path.read_text(encoding="utf-8"))


async def _queue_alternative(item: DeviceInputItem, frame_id: int) -> bool:
    if int(item.metadata.get("preview_count") or 0) >= 2:
        return False
    valid_ids = {candidate.get("frame_id") for candidate in item.frame_candidates}
    if frame_id not in valid_ids or frame_id == item.metadata.get("preview_frame_id"):
        return False
    await DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="observation_alternative_preview",
        payload={
            "item_id": str(item.id),
            "frame_id": frame_id,
            "width": 640,
            "preview_index": 2,
        },
    ).insert()
    item.metadata = {**item.metadata, "alternative_preview_requested": True}
    return True


async def _ensure_preview_retry(item: DeviceInputItem) -> None:
    if int(item.metadata.get("preview_count") or 0) >= 1:
        return
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == item.source_id,
        DeviceInputJob.kind == "thumbnail",
        {"payload.item_id": str(item.id)},
        {"status": {"$in": ["pending", "claimed"]}},
    )
    if existing or not item.frame_candidates:
        return
    await DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="observation_preview_retry",
        payload={
            "item_id": str(item.id),
            "frame_id": item.frame_candidates[0]["frame_id"],
            "width": 640,
            "preview_index": 1,
        },
    ).insert()


async def _queue_source_media(item: DeviceInputItem) -> None:
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == item.source_id,
        DeviceInputJob.kind == "source_media",
        {"payload.item_id": str(item.id)},
        {"status": {"$in": ["pending", "claimed", "complete"]}},
    )
    if existing:
        return
    frame_id = item.metadata.get("preview_frame_id")
    if frame_id is None:
        frame_id = item.frame_candidates[0]["frame_id"]
    await DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="source_media",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="observation_vault_media",
        payload={
            "item_id": str(item.id),
            "frame_id": frame_id,
            "width": 1280,
            "preview_index": int(item.metadata.get("preview_count") or 1),
        },
    ).insert()


async def _immich_image(
    asset_id: str, endpoint: str, *, max_bytes: int = 25 * 1024 * 1024
) -> tuple[bytes, str]:
    base = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    if not base or not key:
        raise RuntimeError("Immich is not configured")
    async with httpx.AsyncClient(
        timeout=60, headers={"x-api-key": key, "Accept": "image/*"}
    ) as client:
        response = await client.get(f"{base}/api/assets/{asset_id}/{endpoint}")
        response.raise_for_status()
        if len(response.content) > max_bytes:
            raise ValueError("Immich image exceeds the curation media limit")
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[
            0
        ]
        if not content_type.startswith("image/"):
            raise ValueError("Immich returned non-image content")
        return response.content, content_type


def _write_media_provenance(
    root: Path,
    digest: str,
    media_path: str,
    item: DeviceInputItem,
    *,
    source_provider: str,
    source_media_id: str,
) -> str:
    notes_dir = root / "Media"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note = notes_dir / f"{digest}.md"
    if not note.exists():
        conversations = ", ".join(item.related_conversation_ids)
        temporary = note.with_suffix(".md.part")
        temporary.write_text(
            "---\n"
            f"source: {source_provider}\n"
            f"source_id: {item.source_id}\n"
            f"source_media_id: {source_media_id}\n"
            f"captured_at: {item.captured_at.isoformat()}\n"
            f"observation_id: {item.id}\n"
            f"conversation_ids: [{conversations}]\n"
            f"content_hash: {digest}\n"
            "---\n\n"
            f"![[../{media_path}]]\n",
            encoding="utf-8",
        )
        os.replace(temporary, note)
    return note.relative_to(root).as_posix()


def _append_vault_observation(
    item: DeviceInputItem,
    decision: dict[str, Any],
    revision: str,
    root: Path,
    promoted_path: str | None,
) -> str:
    requested = decision.get("note_path")
    if decision["decision"] == "text_update":
        requested = None
    note = safe_note_path(root, requested, item.captured_at)
    note.parent.mkdir(parents=True, exist_ok=True)
    marker = f"<!-- observation:{item.id}:{revision} -->"
    existing = note.read_text(encoding="utf-8") if note.exists() else ""
    if marker in existing:
        return note.relative_to(root).as_posix()
    heading = (
        decision.get("title") or item.metadata.get("window_name") or "Screen context"
    )
    timestamp = _as_utc(item.captured_at).strftime("%H:%M")
    lines = [f"## {timestamp} — {heading}", "", decision.get("summary", "").strip()]
    facts = [
        str(fact).strip() for fact in decision.get("facts", []) if str(fact).strip()
    ]
    if facts:
        lines.extend(["", *[f"- {fact}" for fact in facts]])
    if promoted_path:
        lines.extend(["", f"![[{promoted_path}]]"])
    if item.related_conversation_ids:
        lines.extend(
            [
                "",
                "Related conversations: "
                + ", ".join(
                    f"[[Conversations/{conversation_id}]]"
                    for conversation_id in item.related_conversation_ids
                ),
            ]
        )
    lines.extend(["", marker, ""])
    temporary = note.with_suffix(note.suffix + ".part")
    temporary.write_text(
        existing.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8"
    )
    os.replace(temporary, note)
    return note.relative_to(root).as_posix()


async def apply_curation_decision(
    item: DeviceInputItem, decision: dict[str, Any], revision: str
) -> bool:
    item.agent_reason = str(decision.get("reason") or "")
    action = decision["decision"]
    if action == "alternative_preview":
        frame_id = decision.get("alternative_frame_id")
        if isinstance(frame_id, int) and await _queue_alternative(item, frame_id):
            item.curation = "pending"
            await item.save()
            return False
        action = "discard"

    if action == "duplicate":
        target_id = decision.get("duplicate_observation_id")
        try:
            target = await DeviceInputItem.get(target_id) if target_id else None
        except Exception:
            target = None
        if (
            target is None
            or target.user_id != item.user_id
            or target.kind != "observation"
            or str(target.id) == str(item.id)
        ):
            raise ValueError("agent selected an invalid duplicate observation")
        item.duplicate_of = str(target.id)
        item.curation = "duplicate"
        if item.lifecycle == "closed":
            item.media_data = None
            item.media_filename = None
            item.media_content_type = None
            item.content_hash = None
    elif action == "discard":
        item.curation = "discarded"
        if item.lifecycle == "closed":
            item.media_data = None
            item.media_filename = None
            item.media_content_type = None
            item.content_hash = None
    else:
        retain_image = bool(decision.get("retain_image")) or action == "promote_image"
        immich_item_id = decision.get("immich_item_id") if retain_image else None
        if (
            retain_image
            and not immich_item_id
            and not item.metadata.get("source_media_available")
        ):
            await _queue_source_media(item)
            item.metadata = {**item.metadata, "promotion_pending": True}
            item.curation = "pending"
            await item.save()
            return False
        root = ConvDocVaultManager().user_root(item.user_id)
        root.mkdir(parents=True, exist_ok=True)
        promoted = None
        provenance_path = None
        if retain_image and immich_item_id:
            try:
                immich_item = await DeviceInputItem.get(immich_item_id)
            except Exception:
                immich_item = None
            if (
                immich_item is None
                or immich_item.user_id != item.user_id
                or immich_item.kind != "immich_memory"
            ):
                raise ValueError("agent selected an invalid Immich candidate")
            asset_id = immich_item.metadata.get("asset_id")
            data, content_type = await _immich_image(str(asset_id), "original")
            promoted, digest = promote_image_bytes(data, content_type, root)
            immich_item.promoted_path = promoted
            immich_item.content_hash = digest
            immich_item.state = "promoted"
            await immich_item.save()
            provenance_path = _write_media_provenance(
                root,
                digest,
                promoted,
                item,
                source_provider="immich",
                source_media_id=str(asset_id),
            )
        elif retain_image:
            if not item.media_data or not item.media_content_type:
                raise ValueError("selected ScreenPipe image is unavailable")
            promoted, digest = promote_image_bytes(
                item.media_data, item.media_content_type, root
            )
            item.content_hash = digest
            item.promoted_path = promoted
            provenance_path = _write_media_provenance(
                root,
                digest,
                promoted,
                item,
                source_provider="screenpipe",
                source_media_id=str(item.metadata.get("preview_frame_id") or ""),
            )
        path = _append_vault_observation(item, decision, revision, root, promoted)
        item.vault_paths = sorted(
            set(
                [
                    *item.vault_paths,
                    path,
                    *([provenance_path] if provenance_path else []),
                ]
            )
        )
        item.curation = "promoted" if promoted else "linked"
        if promoted:
            item.state = "promoted"
    item.curation_revision = revision
    item.curated_at = utcnow()
    await item.save()
    return True


async def process_observation_curation() -> dict[str, Any]:
    pending = await DeviceInputItem.find(
        DeviceInputItem.kind == "observation",
        DeviceInputItem.curation == "pending",
    ).to_list()
    processed = 0
    waiting = 0
    failed = 0
    now = utcnow()
    for item in pending:
        revision = observation_revision(item)
        if item.curation_revision == revision:
            continue
        if (
            item.lifecycle == "open"
            and item.curated_at is not None
            and _as_utc(item.curated_at) > now - _OPEN_CURATION_INTERVAL
        ):
            continue
        visual_expected = bool(item.frame_candidates) and not item.metadata.get(
            "inactive"
        )
        if visual_expected and not item.media_data:
            await _ensure_preview_retry(item)
            waiting += 1
            continue
        item.curation = "curating"
        await item.save()
        try:
            audio, immich, duplicate_candidates = await _related_context(item)
            root = ConvDocVaultManager().user_root(item.user_id)
            decision = await run_codex_observation_agent(
                item, root, audio, immich, duplicate_candidates
            )
            if await apply_curation_decision(item, decision, revision):
                processed += 1
        except Exception as exc:
            available, _ = codex_executor_available()
            if not available:
                item.curation = "pending"
                waiting += 1
                logger.info("observation %s awaits Codex: %s", item.id, exc)
            else:
                item.curation = "failed"
                item.agent_reason = str(exc)[:2000]
                failed += 1
                logger.exception("observation %s curation failed", item.id)
            await item.save()
    return {
        "pending": len(pending),
        "processed": processed,
        "waiting": waiting,
        "failed": failed,
    }
