#!/usr/bin/env python3
"""Administrative CLI for Chronicle data archives and memory reconstruction."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from advanced_omi_backend.database import get_database
from advanced_omi_backend.services.data_archive import (
    ARCHIVE_SUFFIX,
    ArchiveError,
    create_data_archive,
    import_data_archive,
    verify_data_archive,
)
from advanced_omi_backend.services.memory.rebuild import (
    TIMELINE_STAGES,
    MemoryRebuildError,
    RebuildStage,
    build_rebuild_plan,
    build_timeline_days,
    execute_memory_rebuild,
)

console = Console()
DATA_DIR = Path("/app/data")


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _default_archive_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return DATA_DIR / "backups" / f"chronicle_{timestamp}{ARCHIVE_SUFFIX}"


def _manifest_table(manifest: dict) -> Table:
    table = Table(title="Archive contents")
    table.add_column("Collection")
    table.add_column("Documents", justify="right")
    for name, metadata in manifest["collections"].items():
        table.add_row(name, str(metadata["documents"]))
    data_files = sum(
        metadata.get("kind") == "data_file" for metadata in manifest["files"].values()
    )
    table.add_section()
    table.add_row("Filesystem files", str(data_files))
    return table


def _require_confirmation(message: str, force: bool) -> None:
    if force:
        return
    console.print(Panel(message, title="Destructive operation", border_style="red"))
    if not Confirm.ask("Proceed?", default=False):
        raise KeyboardInterrupt


async def _connect_database():
    database = get_database()
    await database.command("ping")
    return database


def _read_id_list(path: Path) -> list[str]:
    ids = [line.strip() for line in path.read_text().splitlines()]
    return [value for value in ids if value and not value.startswith("#")]


async def _run_export(args: argparse.Namespace) -> None:
    database = await _connect_database()
    output = args.output or _default_archive_path()
    excluded = _read_id_list(args.exclude_audio_for) if args.exclude_audio_for else []
    with console.status("Exporting Chronicle data..."):
        summary = await create_data_archive(
            database,
            output,
            data_dir=args.data_dir,
            overwrite=args.overwrite,
            exclude_audio_conversation_ids=excluded,
        )
    excluded_note = ""
    if summary.excluded_audio_conversations:
        excluded_note = (
            f"\n[yellow]Audio omitted:[/yellow] {summary.excluded_audio_chunks} chunks "
            f"from {summary.excluded_audio_conversations} conversations already held by "
            "an earlier backup; restore those alongside it."
        )
    console.print(
        Panel(
            f"[green]Archive created[/green]\n"
            f"Path: {summary.path}\n"
            f"Collections: {summary.collections}\n"
            f"Documents: {summary.documents}\n"
            f"Filesystem files: {summary.files}\n"
            f"Archive size: {_human_size(summary.bytes_written)}"
            f"{excluded_note}",
            border_style="green",
        )
    )


async def _run_verify(args: argparse.Namespace) -> None:
    with console.status("Verifying archive checksums..."):
        manifest = verify_data_archive(args.archive)
    console.print(_manifest_table(manifest))
    console.print("[green]Archive verification passed.[/green]")


async def _rebuild(database, args: argparse.Namespace):
    from_stage = RebuildStage(args.rebuild_from)
    plan = await build_rebuild_plan(
        database,
        args.user_id,
        from_stage=from_stage,
    )
    console.print(
        f"Rebuild plan from {from_stage.value}: {plan.speaker_count} speaker inputs, "
        f"{plan.memory_count} memory inputs across {len(plan.user_ids)} users."
    )
    if from_stage is RebuildStage.SPEAKERS:
        skipped_count = plan.count - plan.speaker_count
        if skipped_count:
            console.print(
                f"[yellow]Speaker stage will skip {skipped_count} transcript-only "
                "conversations with no stored audio.[/yellow]"
            )
    if from_stage in TIMELINE_STAGES:
        days = await build_timeline_days(database, plan.user_ids)
        diarize = (
            "re-diarizing every recording first"
            if from_stage is RebuildStage.TIMELINE
            else "reusing the speaker transcripts already on those recordings"
        )
        console.print(
            f"{from_stage.value.title()} stage will re-analyse {len(days)} local "
            f"day(s) with captured audio and record each one, {diarize}; the "
            "per-conversation memory path does not run."
        )
    if getattr(args, "dry_run", False):
        return None
    extra = (
        " Existing timeline runs, days, and episodes are deleted so analysis starts "
        "from evidence rather than from the boundaries being replaced."
        if from_stage in TIMELINE_STAGES
        else ""
    )
    _require_confirmation(
        "This deletes the selected users' current Markdown memory vaults and memory "
        "audit history, then recreates them from active transcripts. Syncthing "
        f"pairing markers are retained.{extra}",
        args.force,
    )
    backup_dir = None if args.no_vault_backup else args.data_dir / "backups"
    result = await execute_memory_rebuild(
        database,
        plan,
        data_dir=args.data_dir,
        backup_dir=backup_dir,
        from_stage=from_stage,
    )
    backup_text = str(result.vault_backup) if result.vault_backup else "not needed"
    console.print(
        Panel(
            f"[green]Memory rebuild queued[/green]\n"
            f"Run ID: {result.run_id}\n"
            f"Speaker jobs: {len(result.speaker_jobs)}\n"
            f"Speaker skipped (no audio): "
            f"{len(result.skipped_speaker_conversations)}\n"
            f"Memory jobs: {len(result.memory_jobs)}\n"
            f"Timeline day jobs: {len(result.timeline_jobs)}\n"
            f"Deleted timeline documents: {result.deleted_timeline_documents}\n"
            f"Users: {len(result.user_ids)}\n"
            f"Deleted vault files: {result.deleted_vault_files}\n"
            f"Deleted audit entries: {result.deleted_audit_entries}\n"
            f"Previous vault backup: {backup_text}\n\n"
            "Stages run chronologically within each user; different users may "
            "rebuild in parallel.",
            border_style="green",
        )
    )
    return result


async def _run_import(args: argparse.Namespace) -> None:
    if args.rebuild_from and args.user_id:
        raise ArchiveError(
            "--user-id cannot be combined with --rebuild-from import because the "
            "archive contains all users. Import first, then run rebuild-memory "
            "--user-id for a selective rebuild."
        )
    with console.status("Verifying archive before import..."):
        manifest = verify_data_archive(args.archive)
    console.print(_manifest_table(manifest))
    destructive = args.replace or args.rebuild_from
    if destructive:
        _require_confirmation(
            "Replace mode clears each archived Mongo collection before restore. Fresh "
            "rebuild mode also deletes current derived vault and audit state.",
            args.force,
        )
        # One confirmation covers the complete import + derived-data rebuild.
        if args.rebuild_from:
            args.force = True

    database = await _connect_database()
    restore_files = not args.database_only and not args.rebuild_from
    with console.status("Importing verified Chronicle archive..."):
        summary = await import_data_archive(
            database,
            args.archive,
            data_dir=args.data_dir,
            replace=args.replace,
            restore_files=restore_files,
            fresh_memory=bool(args.rebuild_from),
        )
    console.print(
        f"[green]Imported {summary.documents} documents from "
        f"{summary.collections} collections and {summary.files} files.[/green]"
    )
    if summary.skipped_collections:
        console.print(
            "Skipped derived collections: " + ", ".join(summary.skipped_collections)
        )
    for warning in summary.duplicate_audio_warnings:
        console.print(
            "[yellow]Duplicate audio skipped:[/yellow] conversation "
            f"{warning.skipped_conversation_id} matches {warning.kept_source} "
            f"conversation {warning.kept_conversation_id}; kept the first copy."
        )
    for warning in summary.duplicate_chunk_warnings:
        console.print(
            "[yellow]Duplicate audio chunk skipped:[/yellow] conversation "
            f"{warning.conversation_id}, chunk {warning.chunk_index}; kept "
            f"{warning.kept_source} chunk {warning.kept_chunk_id}."
        )
    if args.rebuild_from:
        await _rebuild(database, args)


async def _run_rebuild(args: argparse.Namespace) -> None:
    database = await _connect_database()
    await _rebuild(database, args)


def _add_common_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Chronicle data directory inside the container (default: /app/data)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export, import, and reconstruct Chronicle's durable data"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Create a full data archive")
    export_parser.add_argument("output", nargs="?", type=Path)
    export_parser.add_argument("--overwrite", action="store_true")
    export_parser.add_argument(
        "--exclude-audio-for",
        type=Path,
        help=(
            "File of conversation ids (one per line) whose audio_chunks to omit "
            "because an earlier backup already holds identical audio; produce it "
            "with scripts/audio_backup_dedup.py scan --exclude-list"
        ),
    )
    _add_common_data_dir(export_parser)
    export_parser.set_defaults(handler=_run_export)

    verify_parser = subparsers.add_parser(
        "verify", help="Verify archive structure and checksums"
    )
    verify_parser.add_argument("archive", type=Path)
    verify_parser.set_defaults(handler=_run_verify)

    import_parser = subparsers.add_parser("import", help="Import a data archive")
    import_parser.add_argument("archive", type=Path)
    import_parser.add_argument(
        "--replace",
        action="store_true",
        help="Clear each archived collection before restoring it",
    )
    import_parser.add_argument(
        "--database-only",
        action="store_true",
        help="Do not restore vault or legacy audio files",
    )
    import_parser.add_argument(
        "--rebuild-from",
        choices=[stage.value for stage in RebuildStage],
        help=(
            "Skip archived derived memory and rebuild from this stage; "
            "'speakers' runs speaker recognition before memory"
        ),
    )
    import_parser.add_argument("--user-id", action="append")
    import_parser.add_argument("--no-vault-backup", action="store_true")
    import_parser.add_argument("--force", action="store_true")
    _add_common_data_dir(import_parser)
    import_parser.set_defaults(handler=_run_import, dry_run=False)

    rebuild_parser = subparsers.add_parser(
        "rebuild-memory", help="Recreate Markdown memories from active transcripts"
    )
    rebuild_parser.add_argument("--user-id", action="append")
    rebuild_parser.add_argument(
        "--rebuild-from",
        choices=[stage.value for stage in RebuildStage],
        default=RebuildStage.MEMORY.value,
        help="Earliest stage to rerun (default: memory)",
    )
    rebuild_parser.add_argument("--dry-run", action="store_true")
    rebuild_parser.add_argument("--no-vault-backup", action="store_true")
    rebuild_parser.add_argument("--force", action="store_true")
    _add_common_data_dir(rebuild_parser)
    rebuild_parser.set_defaults(handler=_run_rebuild)
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    await args.handler(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except (ArchiveError, MemoryRebuildError, OSError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)
