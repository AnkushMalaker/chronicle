"""Content-addressed image storage under a vault's ``_media/`` directory."""

import hashlib
import os
from pathlib import Path

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
