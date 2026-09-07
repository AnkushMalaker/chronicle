"""Speaker mining: ingest an unlabelled audio corpus and score it for one speaker.

Backs the "mine more audio" flow on the speaker-enhancement page. Server-side
audio files (e.g. backup WAVs whose conversations were purged) are ingested
through the standard upload pipeline as annotation-only conversations — audio
chunks in Mongo, batch transcription, speaker identification, no memory
extraction — and a corpus-discovery job is chained behind the transcription
jobs so the new speech is embedded and scored against the target speaker's
gallery as soon as it has segments. Mined clips then surface in guided
enrollment like any other corpus match.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import JSONResponse
from rq import get_current_job
from rq.job import Dependency
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.controllers.queue_controller import JOB_RESULT_TTL, default_queue
from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.models.user import get_user_by_id
from backend.workers.speaker_discovery_jobs import discover_speaker_candidates_job

logger = logging.getLogger(__name__)

# Server-side ingest only reads inside the mounted data dir.
ALLOWED_ROOT = Path("/app/data")
INGEST_BATCH_SIZE = 8
MINING_DEVICE_NAME = "speaker-mining"


def _progress(current: int, total: int, message: str) -> None:
    job = get_current_job()
    if not job:
        return
    job.meta["batch_progress"] = {
        "current": current,
        "total": total,
        "percent": round(current * 100 / total) if total else 100,
        "message": message,
    }
    job.save_meta()


def _parse_upload_response(result) -> dict:
    if isinstance(result, JSONResponse):
        return json.loads(result.body)
    return result


async def enqueue_discovery_after(
    requested_by: str,
    speaker_id: str,
    speaker_name: str,
    depends_on_job_ids: list[str],
    include_all_users: bool,
    include_deleted: bool = False,
) -> str:
    """Queue corpus discovery behind the ingest's transcription jobs.

    ``allow_failure`` keeps discovery running even if some files fail to
    transcribe — partial corpus coverage beats none.
    """
    kwargs = dict(
        requested_by=requested_by,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        include_all_users=include_all_users,
        include_deleted=include_deleted,
        job_timeout=14400,
        result_ttl=JOB_RESULT_TTL,
        description=f"Speaker corpus discovery: {speaker_name} (after mining ingest)",
    )
    if depends_on_job_ids:
        kwargs["depends_on"] = Dependency(jobs=depends_on_job_ids, allow_failure=True)
    job = default_queue.enqueue(discover_speaker_candidates_job, **kwargs)
    # Record the run so the enhancement page reattaches to it on refresh.
    database = Conversation.get_pymongo_collection().database
    await database["speaker_discovery_runs"].update_one(
        {"requested_by": requested_by, "speaker_id": speaker_id},
        {
            "$set": {
                "speaker_name": speaker_name,
                "job_id": job.id,
                "queued_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return job.id


@async_job(redis=False, beanie=True, timeout=14400)
async def mine_local_corpus_job(
    requested_by: str,
    speaker_id: str,
    speaker_name: str,
    paths: list[str],
    include_all_users: bool = False,
) -> dict:
    """Ingest server-side audio files and chain discovery for one speaker."""
    # Lazy import: audio_controller pulls in the transcription stack, which the
    # guided-enrollment controller (importing this module's enqueue helper's
    # sibling) must not load at import time.
    from backend.controllers.audio_controller import upload_and_process_audio_files

    user = await get_user_by_id(requested_by)
    if user is None:
        raise RuntimeError(f"Unknown user {requested_by}")

    root = ALLOWED_ROOT.resolve()
    valid: list[Path] = []
    skipped: list[dict] = []
    for raw in paths:
        path = Path(raw).resolve()
        if not str(path).startswith(str(root) + "/"):
            skipped.append({"path": raw, "error": "outside the data directory"})
            continue
        if not path.is_file():
            skipped.append({"path": raw, "error": "not a file"})
            continue
        valid.append(path)

    transcript_job_ids: list[str] = []
    conversation_ids: list[str] = []
    failed: list[dict] = []
    processed = 0
    for offset in range(0, len(valid), INGEST_BATCH_SIZE):
        batch = valid[offset : offset + INGEST_BATCH_SIZE]
        handles = [path.open("rb") for path in batch]
        try:
            uploads = [
                StarletteUploadFile(file=handle, filename=path.name)
                for handle, path in zip(handles, batch)
            ]
            result = _parse_upload_response(
                await upload_and_process_audio_files(
                    user,
                    uploads,
                    device_name=MINING_DEVICE_NAME,
                    annotation_only=True,
                )
            )
        finally:
            for handle in handles:
                handle.close()
        for item in result.get("files", []):
            if item.get("status") == "started":
                conversation_ids.append(item.get("conversation_id"))
                if item.get("transcript_job_id"):
                    transcript_job_ids.append(item["transcript_job_id"])
            else:
                failed.append(
                    {"path": item.get("filename"), "error": item.get("error")}
                )
        processed += len(batch)
        _progress(
            processed,
            len(valid),
            f"Ingested {processed}/{len(valid)} files for {speaker_name}",
        )

    discovery_job_id = None
    if conversation_ids:
        discovery_job_id = await enqueue_discovery_after(
            requested_by,
            speaker_id,
            speaker_name,
            transcript_job_ids,
            include_all_users,
        )

    if not transcript_job_ids and conversation_ids:
        logger.warning(
            "Speaker mining: no transcription provider available; %d conversations "
            "ingested without transcripts and will not be discoverable until "
            "transcribed",
            len(conversation_ids),
        )

    return {
        "speaker_name": speaker_name,
        "requested_files": len(paths),
        "ingested": len(conversation_ids),
        "transcription_jobs": len(transcript_job_ids),
        "discovery_job_id": discovery_job_id,
        "failed": failed[:20],
        "skipped": skipped[:20],
    }
