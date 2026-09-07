"""Deterministic photo coverage and labelled contact sheets, without model decisions."""

from __future__ import annotations

import io
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps


def instant(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def photo_metadata(asset: dict[str, Any]) -> dict[str, Any]:
    """Retain source facts separately from later pixel-derived observations."""
    exif = asset.get("exifInfo") or {}
    return {
        "asset_id": str(asset["id"]),
        "filename": asset.get("originalFileName") or "",
        "captured_at": asset.get("fileCreatedAt"),
        "server_created_at": asset.get("createdAt"),
        "source_updated_at": asset.get("updatedAt"),
        "source_timezone": exif.get("timeZone"),
        "type": asset.get("type"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "latitude": exif.get("latitude"),
        "longitude": exif.get("longitude"),
        "city": exif.get("city"),
        "country": exif.get("country"),
        "camera_make": exif.get("make"),
        "camera_model": exif.get("model"),
        "description": exif.get("description"),
        "duplicate_id": asset.get("duplicateId"),
        "live_photo_video_id": asset.get("livePhotoVideoId"),
        "people": [
            {"id": str(p["id"]), "name": p.get("name") or ""}
            for p in asset.get("people", [])
            if p.get("id")
        ],
        "is_favorite": bool(asset.get("isFavorite")),
        "is_archived": bool(asset.get("isArchived")),
    }


def _features(item: dict) -> set[str]:
    result = {"person:" + p["id"] for p in item.get("people", [])}
    if item.get("city"):
        result.add("city:" + item["city"])
    if item.get("latitude") is not None and item.get("longitude") is not None:
        result.add(
            f"location:{round(item['latitude'], 2)}:{round(item['longitude'], 2)}"
        )
    if item.get("is_favorite"):
        result.add("favorite")
    return result


def sample_photos(
    catalog: Iterable[dict], limit: int = 12, *, seen: Iterable[str] = ()
) -> list[dict]:
    """Half uniform chronological quantiles, half gap/metadata diversity.

    Duplicate membership remains in the inventory. Do not label time-adjacent photos
    as duplicates: different moments within a burst can explain an event transition.
    """
    if limit <= 0:
        return []
    seen = set(seen)
    rows = sorted(
        (x for x in catalog if x["asset_id"] not in seen),
        key=lambda x: (instant(x["captured_at"]), x["asset_id"]),
    )
    unique, groups = [], set()
    for row in rows:
        group = row.get("duplicate_id")
        if group and group in groups:
            continue
        unique.append(row)
        if group:
            groups.add(group)
    rows = unique
    if len(rows) <= limit:
        return rows
    n_uniform = max(2, math.ceil(limit / 2)) if limit > 1 else 1
    indices = [
        round(i * (len(rows) - 1) / max(1, n_uniform - 1)) for i in range(n_uniform)
    ]
    chosen = [rows[i] for i in indices]
    span = max(
        1,
        (
            instant(rows[-1]["captured_at"]) - instant(rows[0]["captured_at"])
        ).total_seconds(),
    )
    features = set().union(*(_features(x) for x in chosen))
    while len(chosen) < limit:
        ids = {x["asset_id"] for x in chosen}

        def score(x):
            gap = (
                min(
                    abs(
                        (
                            instant(x["captured_at"]) - instant(y["captured_at"])
                        ).total_seconds()
                    )
                    for y in chosen
                )
                / span
            )
            return (len(_features(x) - features) + gap, x["asset_id"])

        row = max((x for x in rows if x["asset_id"] not in ids), key=score)
        chosen.append(row)
        features.update(_features(row))
    return sorted(chosen, key=lambda x: (instant(x["captured_at"]), x["asset_id"]))


def catalog_summary(catalog: list[dict], zone: str) -> dict:
    local = [instant(x["captured_at"]).astimezone(ZoneInfo(zone)) for x in catalog]
    return {
        "asset_count": len(catalog),
        "by_hour": dict(sorted(Counter(x.strftime("%H") for x in local).items())),
        "with_people": sum(bool(x.get("people")) for x in catalog),
        "with_gps": sum(
            x.get("latitude") is not None and x.get("longitude") is not None
            for x in catalog
        ),
        "duplicate_members": sum(bool(x.get("duplicate_id")) for x in catalog),
        "first_capture": min(local).isoformat() if local else None,
        "last_capture": max(local).isoformat() if local else None,
    }


def thumbnail_grid(images: list[tuple[dict, bytes]], zone: str) -> bytes:
    """Render real pixels with stable tile labels; never synthesize image content."""
    if not images or len(images) > 12:
        raise ValueError("A grid needs 1–12 images")
    columns = min(4, len(images))
    tile_w, tile_h = 320, 292
    canvas = Image.new(
        "RGB", (columns * tile_w, math.ceil(len(images) / columns) * tile_h), "#161b22"
    )
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    for i, (meta, content) in enumerate(images):
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Image dimensions exceed grid limit")
            frame = ImageOps.contain(
                ImageOps.exif_transpose(image).convert("RGB"), (304, 236)
            )
        x, y = (i % columns) * tile_w, (i // columns) * tile_h
        canvas.paste(
            frame, (x + (tile_w - frame.width) // 2, y + (240 - frame.height) // 2)
        )
        stamp = (
            instant(meta["captured_at"])
            .astimezone(ZoneInfo(zone))
            .strftime("%d %b %Y %H:%M:%S")
        )
        draw.text((x + 8, y + 244), f"T{i+1:02d}  {stamp}", font=font, fill="white")
        draw.text((x + 8, y + 265), meta["asset_id"][:24], font=font, fill="#b8c4d4")
    stream = io.BytesIO()
    canvas.save(stream, format="PNG")
    return stream.getvalue()
