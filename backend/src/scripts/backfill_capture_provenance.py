#!/usr/bin/env python3
"""Backfill required capture provenance during the one-shot offline cutover.

Dry-run is the default. Applying requires both ``--apply`` and
``--confirm-writers-stopped``. The command changes only four provenance fields on
historical ``audio_capture_sessions`` and refuses ambiguous origins, conflicting
partial data, pre-maintenance active sessions, or a missing vault verification root.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import BSON, json_util

from backend.database import get_database
from backend.models.audio_capture import CaptureEffects

PROVENANCE_FIELDS = (
    "capture_epoch",
    "processing_profile",
    "effects",
    "voice_session_id",
)
PROTECTED_COLLECTIONS = ("audio_chunks", "conversations")
INTERACTIVE_PROFILES = {"duplex_aec", "duplex_isolated", "half_duplex"}


@dataclass(frozen=True)
class BackfillPlan:
    document_id: Any
    capture_session_id: str
    origin: str
    profile: str
    updates: dict[str, Any]


def _expected_provenance(origin: str) -> dict[str, Any]:
    if origin == "streaming":
        profile = "ambient"
        effects = CaptureEffects.unreported()
    elif origin in {"upload", "batch", "import"}:
        profile = "imported"
        effects = CaptureEffects.not_applicable()
    elif origin == "screenpipe":
        profile = "source_native"
        effects = CaptureEffects.unreported()
    else:
        raise ValueError(f"unsupported historical capture origin: {origin!r}")
    return {
        "capture_epoch": 0,
        "processing_profile": profile,
        "effects": effects.model_dump(mode="json"),
        "voice_session_id": None,
    }


def _canonical(value: Any) -> Any:
    return json.loads(
        json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS)
    )


def plan_document(document: dict[str, Any]) -> BackfillPlan | None:
    """Return only missing historical fields; reject conflicting partial data."""

    missing = [field for field in PROVENANCE_FIELDS if field not in document]
    if not missing:
        validate_provenance(document)
        return None
    expected = _expected_provenance(str(document.get("origin") or ""))
    conflicts = [
        field
        for field in PROVENANCE_FIELDS
        if field in document
        and _canonical(document[field]) != _canonical(expected[field])
    ]
    if conflicts:
        raise ValueError(
            f"capture {document.get('capture_session_id')} has conflicting partial "
            f"provenance: {', '.join(conflicts)}"
        )
    return BackfillPlan(
        document_id=document["_id"],
        capture_session_id=str(document.get("capture_session_id") or ""),
        origin=str(document["origin"]),
        profile=expected["processing_profile"],
        updates={field: expected[field] for field in missing},
    )


def validate_provenance(document: dict[str, Any]) -> None:
    missing = [field for field in PROVENANCE_FIELDS if field not in document]
    if missing:
        raise ValueError("missing capture provenance: " + ", ".join(missing))
    epoch = document["capture_epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("capture_epoch must be a non-negative integer")
    profile = document["processing_profile"]
    if profile not in {
        "ambient",
        "imported",
        "source_native",
        *INTERACTIVE_PROFILES,
    }:
        raise ValueError(f"invalid processing_profile: {profile!r}")
    effects = CaptureEffects.model_validate(document["effects"])
    voice_session_id = document["voice_session_id"]
    if profile in INTERACTIVE_PROFILES:
        if not voice_session_id or not effects.is_reported:
            raise ValueError(
                "interactive provenance requires voice identity and effects"
            )
    elif voice_session_id is not None:
        raise ValueError("non-interactive provenance cannot bind a voice session")
    if profile == "imported" and (
        epoch != 0 or effects != CaptureEffects.not_applicable()
    ):
        raise ValueError("imported provenance must be epoch zero/not-applicable")
    if profile == "source_native" and epoch != 0:
        raise ValueError("source-native provenance must be epoch zero")


async def _collection_digest(collection, *, exclude: set[str] | None = None) -> str:
    digest = hashlib.sha256()
    cursor = collection.find({}).sort("_id", 1)
    async for document in cursor:
        for field in exclude or set():
            document.pop(field, None)
        digest.update(BSON.encode(document))
    return digest.hexdigest()


def _vault_digest(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    files = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files += 1
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return files, digest.hexdigest()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("maintenance start must include a UTC offset")
    return parsed.astimezone(timezone.utc)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    database = get_database()
    captures = database["audio_capture_sessions"]
    documents = await captures.find({}).sort("_id", 1).to_list(length=None)
    plans: list[BackfillPlan] = []
    errors: list[str] = []
    complete = 0
    for document in documents:
        try:
            plan = plan_document(document)
            if plan is None:
                complete += 1
            else:
                plans.append(plan)
        except (KeyError, TypeError, ValueError) as error:
            errors.append(str(error))

    maintenance_start = _parse_utc(args.maintenance_start)
    old_active = await captures.count_documents(
        {"status": "active", "started_at": {"$lt": maintenance_start}}
    )
    profile_counts = Counter(plan.profile for plan in plans)
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "maintenance_start": maintenance_start.isoformat(),
        "capture_sessions": len(documents),
        "already_complete": complete,
        "planned_updates": len(plans),
        "planned_profiles": dict(sorted(profile_counts.items())),
        "old_active_sessions": old_active,
        "errors": errors,
        "applied_updates": 0,
        "postconditions_verified": False,
    }
    if errors:
        raise RuntimeError(json.dumps(report, indent=2, default=str))
    if not args.apply:
        return report
    if not args.confirm_writers_stopped:
        raise RuntimeError("--apply requires --confirm-writers-stopped")
    if old_active:
        raise RuntimeError(
            f"refusing apply: {old_active} capture sessions active before maintenance"
        )
    vault_root = Path(args.vault_root).resolve() if args.vault_root else None
    if vault_root is None or not vault_root.is_dir():
        raise RuntimeError("--apply requires an existing --vault-root")

    before = {
        name: await _collection_digest(database[name]) for name in PROTECTED_COLLECTIONS
    }
    before["capture_immutable"] = await _collection_digest(
        captures, exclude=set(PROVENANCE_FIELDS)
    )
    vault_before = _vault_digest(vault_root)
    for plan in plans:
        conflict_guards = [
            (
                {field: {"$exists": False}}
                if field in plan.updates
                else {field: _expected_provenance(plan.origin)[field]}
            )
            for field in PROVENANCE_FIELDS
        ]
        result = await captures.update_one(
            {"_id": plan.document_id, "$and": conflict_guards},
            {"$set": plan.updates},
        )
        if result.matched_count != 1 or result.modified_count != 1:
            raise RuntimeError(
                f"concurrent or partial update detected for {plan.capture_session_id}"
            )
        report["applied_updates"] += 1

    async for document in captures.find({}):
        validate_provenance(document)
    after = {
        name: await _collection_digest(database[name]) for name in PROTECTED_COLLECTIONS
    }
    after["capture_immutable"] = await _collection_digest(
        captures, exclude=set(PROVENANCE_FIELDS)
    )
    vault_after = _vault_digest(vault_root)
    if before != after or vault_before != vault_after:
        raise RuntimeError(
            "protected audio, claims, capture identity, or vault changed"
        )
    if await captures.count_documents(
        {"status": "active", "started_at": {"$lt": maintenance_start}}
    ):
        raise RuntimeError("pre-maintenance active capture remains after backfill")
    report["protected_digests"] = after
    report["vault_files"] = vault_after[0]
    report["vault_sha256"] = vault_after[1]
    report["postconditions_verified"] = True
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    parser.add_argument("--maintenance-start", required=True)
    parser.add_argument("--vault-root")
    parser.add_argument("--report")
    args = parser.parse_args()
    report = await run(args)
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
