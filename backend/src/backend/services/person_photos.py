"""Embed Immich face photos into the vault's People notes.

When speaker recognition (or the memory agent) creates a ``People/<Name>.md``
note, this daily cron looks the person up in the Immich photo library by name
(``GET /api/search/person``), pulls their face-crop thumbnail
(``GET /api/people/{id}/thumbnail``), stores it content-addressed under the
vault's ``_media/`` directory, and embeds a small photo at the top of the note.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from backend.services.immich_discovery import resolve_immich_user_id
from backend.services.memory.vault_lock import vault_note_lock
from backend.services.memory.vault_manager import ConvDocVaultManager
from backend.services.memory.vault_media import promote_image_bytes, sniff_image_type

logger = logging.getLogger(__name__)

_EMBED_MARKER = "![[../_media/"
_THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024


def match_person(name: str, people: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the Immich person a vault note name refers to, or None.

    An exact (casefolded) name match wins. Otherwise a vault first name matches
    a single Immich full name that starts with it ("Alex" → "Alex Morgan");
    multiple such candidates are ambiguous and match nothing.
    """
    wanted = name.casefold().strip()
    named = [
        person
        for person in people
        if str(person.get("name") or "").strip() and not person.get("isHidden")
    ]
    exact = [p for p in named if p["name"].casefold().strip() == wanted]
    if exact:
        return exact[0]
    prefixed = [p for p in named if p["name"].casefold().startswith(wanted + " ")]
    return prefixed[0] if len(prefixed) == 1 else None


def has_photo_embed(text: str) -> bool:
    return _EMBED_MARKER in text


def embed_photo(text: str, media_path: str) -> str:
    """Insert a small photo embed right below the note's frontmatter."""
    embed = f"![[../{media_path}|200]]\n"
    if text.startswith("---\n"):
        closing = text.find("\n---\n", 4)
        if closing != -1:
            insert_at = closing + len("\n---\n")
            return text[:insert_at] + embed + text[insert_at:]
    return embed + text


def _locked_embed(user_id: str, note: Path, media_path: str) -> bool:
    """Embed the photo under the per-user vault lock; False if already present."""
    with vault_note_lock(user_id):
        current = note.read_text(encoding="utf-8")
        if has_photo_embed(current):
            return False
        note.write_text(embed_photo(current, media_path), encoding="utf-8")
        return True


async def sync_person_photos() -> dict[str, Any]:
    url = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    if not url or not key:
        return {
            "status": "disabled",
            "reason": "IMMICH_URL and IMMICH_API_KEY are required",
        }
    user_id = await resolve_immich_user_id()
    if user_id is None:
        return {"status": "disabled", "reason": "no IMMICH_USER_ID and no admin user"}

    people_dir = ConvDocVaultManager().user_root(user_id) / "People"
    root = people_dir.parent
    if not people_dir.is_dir():
        return {"status": "ok", "people": 0, "embedded": 0}

    notes = sorted(people_dir.glob("*.md"))
    embedded = present = unmatched = 0
    async with httpx.AsyncClient(timeout=30, headers={"x-api-key": key}) as client:
        for note in notes:
            name = note.stem
            # Diarization placeholders are not people (see the memory agent prompt).
            if name.startswith("Unknown Speaker"):
                continue
            if has_photo_embed(note.read_text(encoding="utf-8")):
                present += 1
                continue
            response = await client.get(
                f"{url}/api/search/person", params={"name": name}
            )
            response.raise_for_status()
            person = match_person(name, response.json())
            if person is None:
                unmatched += 1
                continue
            try:
                thumbnail = await client.get(
                    f"{url}/api/people/{person['id']}/thumbnail"
                )
                thumbnail.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Immich thumbnail for %s unavailable: %s", name, exc)
                continue
            data = thumbnail.content
            if not data or len(data) > _THUMBNAIL_MAX_BYTES:
                logger.warning("Immich thumbnail for %s has unusable size", name)
                continue
            # The people thumbnail endpoint declares application/octet-stream, so
            # sniff the actual encoding when the header is not an image type.
            content_type = thumbnail.headers.get("content-type", "").split(";", 1)[0]
            if not content_type.startswith("image/"):
                content_type = sniff_image_type(data) or ""
            try:
                media_path, _digest = promote_image_bytes(data, content_type, root)
            except ValueError as exc:
                logger.warning("Immich thumbnail for %s not usable: %s", name, exc)
                continue
            if await asyncio.to_thread(_locked_embed, user_id, note, media_path):
                embedded += 1
                logger.info("Embedded Immich photo into People/%s.md", name)
            else:
                present += 1
    return {
        "status": "ok",
        "people": len(notes),
        "embedded": embedded,
        "already_present": present,
        "unmatched": unmatched,
    }
