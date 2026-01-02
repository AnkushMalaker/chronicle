
import logging
import os
import uuid
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body
from rq.exceptions import NoSuchJobError
from rq.job import Job
from pydantic import BaseModel
import zipfile

from advanced_omi_backend.auth import current_active_user, current_superuser
from advanced_omi_backend.controllers.queue_controller import default_queue, redis_conn
from advanced_omi_backend.users import User
from advanced_omi_backend.services.obsidian_service import obsidian_service
from advanced_omi_backend.utils.file_utils import extract_zip, ZipExtractionError
from advanced_omi_backend.workers.obsidian_jobs import (
    count_markdown_files,
    ingest_obsidian_vault_job,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/obsidian", tags=["obsidian"])

class IngestRequest(BaseModel):
    vault_path: str

@router.post("/ingest")
async def ingest_obsidian_vault(
    request: IngestRequest,
    current_user: User = Depends(current_active_user)
):
    """
    Immediate/synchronous ingestion endpoint (legacy). Not recommended for UI.
    Prefer the upload_zip + start endpoints to enable progress reporting.
    """
    if not os.path.exists(request.vault_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {request.vault_path}")

    try:
        result = await obsidian_service.ingest_vault(request.vault_path)
        return {"message": "Ingestion complete", **result}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload_zip")
async def upload_obsidian_zip(
    file: UploadFile = File(...),
    current_user: User = Depends(current_superuser)
):
    """
    Upload a zipped Obsidian vault. Returns a job_id that can be started later.
    Uses upload_files_async pattern from upload_files.py for proper file handling.
    """
    if not file.filename.lower().endswith('.zip'):
        raise HTTPException(status_code=400, detail="Please upload a .zip file of your Obsidian vault")

    job_id = str(uuid.uuid4())
    base_dir = Path("/app/data/obsidian_jobs")
    base_dir.mkdir(parents=True, exist_ok=True)
    job_dir = base_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    zip_path = job_dir / "vault.zip"
    extract_dir = job_dir / "vault"
    
    # Use upload_files_async pattern for proper file handling with cleanup
    zip_file_handle = None
    try:
        # Read file content
        file_content = await file.read()
        
        # Save zip file using proper file handling pattern from upload_files_async
        try:
            zip_file_handle = open(zip_path, 'wb')
            zip_file_handle.write(file_content)
        except IOError as e:
            logger.error(f"Error writing zip file {zip_path}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save uploaded zip: {e}")
        
        # Extract zip file using utility function
        try:
            extract_zip(zip_path, extract_dir)
        except zipfile.BadZipFile as e:
            logger.exception(f"Invalid zip file: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid zip file: {e}")
        except ZipExtractionError as e:
            logger.error(f"Error extracting zip file: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to extract zip file: {e}")

        total = count_markdown_files(str(extract_dir))
        
        # Store pending job state in Redis
        pending_state = {
            "status": "ready",
            "total": total,
            "processed": 0,
            "errors": [],
            "vault_path": str(extract_dir),
            "job_id": job_id
        }
        redis_conn.set(f"obsidian_pending:{job_id}", json.dumps(pending_state), ex=3600*24) # 24h expiry

        return {"job_id": job_id, "vault_path": str(extract_dir), "total_files": total}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to process uploaded zip: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process uploaded zip: {e}")
    finally:
        # Ensure file handle is closed (following upload_files_async pattern)
        if zip_file_handle:
            try:
                zip_file_handle.close()
            except Exception as close_error:
                logger.warning(f"Failed to close zip file handle: {close_error}")


@router.post("/start")
async def start_ingestion(
    job_id: str = Body(..., embed=True),
    current_user: User = Depends(current_active_user)
):
    # Check if job is pending
    pending_key = f"obsidian_pending:{job_id}"
    pending_data = redis_conn.get(pending_key)
    
    if pending_data:
        try:
            job_data = json.loads(pending_data)
            vault_path = job_data.get("vault_path")
            
            # Enqueue to RQ
            rq_job = default_queue.enqueue(
                ingest_obsidian_vault_job,
                job_id,  # arg1
                vault_path,  # arg2
                job_id=job_id,  # Set RQ job ID to match our ID
                description=f"Obsidian ingestion for job {job_id}",
                job_timeout=3600  # 1 hour timeout
            )
            
            # Remove pending key
            redis_conn.delete(pending_key)
            
            return {"message": "Ingestion started", "job_id": job_id, "rq_job_id": rq_job.id}
        except Exception as e:
            logger.exception(f"Failed to start job {job_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to start job: {e}")
            
    # Check if already in RQ
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        status = job.get_status()
        if status in ("queued", "started", "deferred", "scheduled"):
             raise HTTPException(status_code=400, detail=f"Job already {status}")
        
        # If finished/failed, we could potentially restart? But for now let's say it's done.
        raise HTTPException(status_code=400, detail=f"Job is in state: {status}")
        
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.get("/status")
async def get_status(job_id: str, current_user: User = Depends(current_active_user)):
    # 1. Try RQ first
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        
        # Get status
        status = job.get_status()
        if status == "started":
            status = "running"
        if status == "canceled":
            status = "cancelled"
            
        # Get metadata
        meta = job.meta or {}
        
        # If meta has status, prefer it (for granular updates)
        if "status" in meta and meta["status"] in ("running", "completed", "failed", "cancelled"):
             status = meta["status"]

        total = meta.get("total_files", 0)
        processed = meta.get("processed", 0)
        percent = int((processed / total) * 100) if total else 0
        
        return {
            "job_id": job_id,
            "status": status,
            "total": total,
            "processed": processed,
            "percent": percent,
            "errors": meta.get("errors", []),
            "vault_path": meta.get("vault_path"),
            "rq_job_id": job.id
        }
        
    except NoSuchJobError:
        # 2. Check pending
        pending_key = f"obsidian_pending:{job_id}"
        pending_data = redis_conn.get(pending_key)
        
        if pending_data:
            try:
                job_data = json.loads(pending_data)
                return {
                    "job_id": job_id,
                    "status": "ready",
                    "total": job_data.get("total", 0),
                    "processed": 0,
                    "percent": 0,
                    "errors": [],
                    "vault_path": job_data.get("vault_path")
                }
            except:
                raise HTTPException(status_code=500, detail="Failed to get job status")
        raise HTTPException(status_code=404, detail="Job not found")


