"""File-backed evidence workspace construction."""

import json
from pathlib import Path

from .contracts import TimelineEvidenceManifest

# Machine bookkeeping the segmentation agent cannot act on: per-10s numeric series,
# content fingerprints, and frame-selection candidates. Measured on one real day these
# were 71% of every window file while the text cap governed only 12%, which is how a
# workspace grew past what one agent pass can read.
_BULK_METADATA_KEYS = frozenset(
    {
        "acoustic_active_fraction",
        "coverage_fraction",
        "frame_candidates",
        "peak_dbfs",
        "rms_dbfs",
        "sample_fingerprints",
        "segments",
        "speech_fraction",
    }
)

# Backstop for values not on the denylist. Semantic metadata (app/window names, URLs,
# ids) is far below this; anything above is a series or a blob.
_MAX_METADATA_VALUE_CHARS = 2000


def _bounded_metadata(metadata: dict) -> dict:
    """Keep metadata the agent can reason about; drop bulk it can only choke on."""

    bounded = {}
    for key, value in metadata.items():
        if key in _BULK_METADATA_KEYS or value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            bounded[key] = value
            continue
        if len(json.dumps(value, default=str)) <= _MAX_METADATA_VALUE_CHARS:
            bounded[key] = value
    return bounded


def write_workspace(
    root: Path,
    manifest: TimelineEvidenceManifest,
    max_text_chars_per_window: int = 30000,
    max_anchor_images_per_window: int = 4,
) -> None:
    """Lay out the day's evidence as files for one agent pass.

    Image bytes are deliberately not written. Measured on a real day they were 7.1MB of
    the workspace and the agent opened none of them — it nominates an episode's
    representative purely from an item's ``image_filename``, and ``_publish`` attaches
    the bytes afterwards from memory, never from here. ``max_anchor_images_per_window``
    still bounds how many items advertise a preview per window.
    """

    windows_dir = root / "windows"
    work_dir = root / "work"
    windows_dir.mkdir(parents=True)
    work_dir.mkdir()

    evidence = {item.evidence_id: item for item in manifest.evidence}
    # Day header only. The full manifest repeated every evidence item with its untruncated
    # excerpt — 7.3MB on a real day — while the prompt directs the agent to the windows.
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "user_id": manifest.user_id,
                "local_date": manifest.local_date.isoformat(),
                "timezone": manifest.timezone,
                "started_at": manifest.started_at.isoformat(),
                "ended_at": manifest.ended_at.isoformat(),
                "evidence_revision": manifest.evidence_revision,
                "window_count": len(manifest.windows),
                "evidence_count": len(manifest.evidence),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (windows_dir / "index.json").write_text(
        json.dumps(
            [window.model_dump(mode="json") for window in manifest.windows], indent=2
        ),
        encoding="utf-8",
    )
    for index, window in enumerate(manifest.windows):
        remaining = max_text_chars_per_window
        bounded_evidence = []
        image_count = 0
        for evidence_id in window.evidence_ids:
            item = evidence[evidence_id].model_dump(mode="json")
            excerpt = item.get("excerpt") or ""
            item["excerpt"] = excerpt[:remaining] or None
            remaining = max(0, remaining - len(item["excerpt"] or ""))
            item["metadata"] = _bounded_metadata(item.get("metadata") or {})
            if item.get("image_filename"):
                image_count += 1
                if image_count > max_anchor_images_per_window:
                    item["image_filename"] = None
            bounded_evidence.append(item)
        payload = {
            "window": window.model_dump(mode="json"),
            "evidence": bounded_evidence,
        }
        (windows_dir / f"{index:04d}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    (root / "README.md").write_text(
        """# Chronicle timeline evidence workspace

Read `windows/index.json`, then inspect every numbered window file in order. The
windows guarantee coverage but are not episode boundaries. Write intermediate notes
under `work/` and the final JSON result to the path specified in the prompt.

No raw audio, image bytes, or credentials are present. An evidence item's
`image_filename` means a preview exists for it and it may be nominated as an episode's
`representative_evidence_id`; the bytes are attached by Chronicle after analysis.
""",
        encoding="utf-8",
    )
