"""File-backed evidence workspace construction."""

import json
from pathlib import Path

from .contracts import TimelineEvidenceManifest


def write_workspace(
    root: Path,
    manifest: TimelineEvidenceManifest,
    images: dict[str, bytes],
    max_text_chars_per_window: int = 30000,
    max_anchor_images_per_window: int = 4,
) -> None:
    windows_dir = root / "windows"
    images_dir = root / "images"
    work_dir = root / "work"
    windows_dir.mkdir(parents=True)
    images_dir.mkdir()
    work_dir.mkdir()

    evidence = {item.evidence_id: item for item in manifest.evidence}
    (root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
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
    for evidence_id, data in images.items():
        safe_name = evidence_id.replace(":", "-")
        content_type = evidence[evidence_id].metadata.get("image_content_type")
        suffix = ".png" if content_type == "image/png" else ".jpg"
        path = images_dir / f"{safe_name}{suffix}"
        path.write_bytes(data)
    (root / "README.md").write_text(
        """# Chronicle timeline evidence workspace

Read `windows/index.json`, then inspect every numbered window file in order. The
windows guarantee coverage but are not episode boundaries. Write intermediate notes
under `work/` and the final JSON result to the path specified in the prompt.

No raw audio or credentials are present. Image files are bounded supporting previews.
""",
        encoding="utf-8",
    )
