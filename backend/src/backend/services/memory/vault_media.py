"""Content-addressed image storage under a vault's ``_media/`` directory."""

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Optional

_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/avif": ".avif",
}


def sniff_image_type(data: bytes) -> str | None:
    """Detect the image content type from magic bytes.

    Needed because some sources (e.g. Immich person thumbnails) declare
    ``application/octet-stream`` regardless of the actual encoding.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def promote_image_bytes(data: bytes, content_type: str, root: Path) -> tuple[str, str]:
    """Write image bytes to ``{root}/_media/{sha256}{suffix}``.

    Returns the vault-relative media path and the content digest. Idempotent:
    an existing file with the same digest is left untouched.
    """
    if not data or not content_type:
        raise ValueError("cannot promote empty image data")
    suffix = _SUFFIXES.get(content_type)
    if suffix is None:
        raise ValueError("unsupported vault image type")
    digest = hashlib.sha256(data).hexdigest()
    media_dir = root / "_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{digest}{suffix}"
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return target.relative_to(root).as_posix(), digest


def _frontmatter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_frontmatter_value(entry) for entry in value) + "]"
    text = str(value).replace("\n", " ").strip()
    return f'"{text}"' if any(char in text for char in ':#"[]{}') else text


def write_media_note(
    media_path: str,
    digest: str,
    root: Path,
    *,
    frontmatter: Mapping[str, Any],
    body: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Write ``{root}/Media/{digest}.md`` embedding ``media_path``.

    Returns the vault-relative note path. Notes live one folder deep, so the embed
    is ``![[../_media/...]]`` — a bare ``_media/`` link does not resolve in Obsidian.

    ``overwrite`` is for the description pass, which writes a placeholder-free note
    only once it has prose; promotion leaves an existing note untouched.
    """
    notes_dir = root / "Media"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{digest}.md"
    if note_path.exists() and not overwrite:
        return note_path.relative_to(root).as_posix()
    lines = ["---"]
    lines += [
        f"{key}: {_frontmatter_value(value)}"
        for key, value in frontmatter.items()
        if value is not None
    ]
    lines += ["---", "", f"![[../{media_path}]]", ""]
    if body:
        lines += [body.strip(), ""]
    temporary = note_path.with_suffix(".md.part")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, note_path)
    return note_path.relative_to(root).as_posix()


def write_manual_memory_note(
    memory_id: str,
    root: Path,
    *,
    frontmatter: Mapping[str, Any],
    media_paths: list[str],
    body: Optional[str] = None,
) -> str:
    """Atomically write one semantic note for a deliberate manual memory."""

    notes_dir = root / "Manual Memories"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{memory_id}.md"
    lines = ["---"]
    lines += [
        f"{key}: {_frontmatter_value(value)}"
        for key, value in frontmatter.items()
        if value is not None
    ]
    lines += ["---", ""]
    lines += [f"![[../{path}]]" for path in media_paths]
    lines.append("")
    lines.append((body or "Manual memory.").strip())
    lines.append("")
    temporary = note_path.with_suffix(".md.part")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, note_path)
    return note_path.relative_to(root).as_posix()
