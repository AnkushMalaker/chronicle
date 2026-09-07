#!/usr/bin/env python3
"""Put hand-authored vault files back after a regeneration wiped them.

A memory rebuild clears the vault and rewrites it from the database, which is correct
for everything the agent produced and wrong for everything it did not. Canvases,
Excalidraw drawings, templates, Bases, and notes typed by hand have no upstream to be
regenerated from: once cleared they exist only inside a ``.chronicle`` archive.

    python src/scripts/restore_vault_files.py ARCHIVE --list
    python src/scripts/restore_vault_files.py ARCHIVE --pattern '*.canvas' --apply

Only files absent from the live vault are written, so a note the agent has since
rewritten is never clobbered — pass ``--overwrite`` to force. Matching is against the
path below ``conversation_docs/<user_id>/``, using shell globbing.
"""

from __future__ import annotations

import argparse
import fnmatch
import zipfile
from pathlib import Path

VAULT_ROOT = Path("/app/data/conversation_docs")
MEMBER_MARKER = "/conversation_docs/"

# Everything the memory agent cannot recreate: it has no database row behind it.
HAND_AUTHORED = (
    "*.canvas",
    "*.base",
    "Excalidraw/*",
    "Templates/*",
    "_media/*",
)


def _members(archive: zipfile.ZipFile) -> dict[str, str]:
    """Vault-relative path -> archive member, for every vault file in the archive."""
    found: dict[str, str] = {}
    for name in archive.namelist():
        if MEMBER_MARKER not in name or name.endswith("/"):
            continue
        found[name.split(MEMBER_MARKER, 1)[1]] = name
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--vault-root", type=Path, default=VAULT_ROOT)
    parser.add_argument(
        "--pattern",
        action="append",
        default=[],
        help="glob below the user directory; repeatable (default: hand-authored files)",
    )
    parser.add_argument("--list", action="store_true", help="show matches and exit")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    patterns = args.pattern or list(HAND_AUTHORED)

    with zipfile.ZipFile(args.archive) as archive:
        members = _members(archive)
        matched = {
            relative: member
            for relative, member in sorted(members.items())
            if any(
                fnmatch.fnmatch(relative.split("/", 1)[-1], pattern)
                for pattern in patterns
            )
        }
        if args.list:
            print(f"{len(members)} vault file(s) in {args.archive.name}")
            for relative in matched:
                print(f"  {relative}")
            return

        written = skipped = 0
        for relative, member in matched.items():
            target = args.vault_root / relative
            if target.exists() and not args.overwrite:
                skipped += 1
                continue
            print(f"  {'restore' if args.apply else 'would restore'}  {relative}")
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
            written += 1

    print(
        f"\n{written} file(s) {'restored' if args.apply else 'to restore'}, "
        f"{skipped} already present"
    )
    if not args.apply:
        print("dry run — nothing written")


if __name__ == "__main__":
    main()
