"""Wake-word data-collection proxy.

Thin, auth-gated proxy in front of the standalone wakeword-service's
data-collection API (its ``/streams``, ``/prime``, ``/samples*`` endpoints). The
browser never talks to the wakeword-service directly — it goes through Chronicle
auth here, and we scope streams/clips to the calling user's own ``client_id``s
(prefix = last 6 of their ObjectId), with admins seeing everything.

The flywheel this serves:
  - ``POST /api/wakeword/prime``  -> "I'll say the wake word now" on a live stream;
    the next utterance is captured as a labeled positive (false-negative mining).
  - ``GET  /api/wakeword/samples`` + ``/label`` -> review captured false-positive
    candidates, marking each true/false so they roll into the negative/positive set.
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wakeword", tags=["wakeword"])

# Standalone wakeword-service, reachable by container name on chronicle-network.
WAKEWORD_SERVICE_URL = os.getenv(
    "WAKEWORD_SERVICE_URL", "http://chronicle-wakeword-service:8770"
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=WAKEWORD_SERVICE_URL, timeout=15.0)


def _suffix(user: User) -> str:
    """Client-id prefix that scopes a user's streams/clips (last 6 of ObjectId)."""
    return user.user_id[-6:]


def _owns(user: User, client_id: str) -> bool:
    return user.is_superuser or (client_id or "").startswith(_suffix(user))


class PrimeRequest(BaseModel):
    # Optional: when omitted, prime the caller's currently-active stream.
    client_id: str | None = None
    # Which wake word to enroll for (required for /prime; ignored by /unprime).
    wakeword: str | None = None


class LabelRequest(BaseModel):
    label: str  # "wake" -> positive, "not_wake" -> negative


@router.get("/models")
async def list_models(current_user: User = Depends(current_active_user)):
    """Wake-word models the service has on disk (for the acoustic-condition picker)."""
    async with _client() as client:
        try:
            resp = await client.get("/models")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/streams")
async def list_streams(current_user: User = Depends(current_active_user)):
    """Active audio streams the caller may prime (their own; all for admins)."""
    async with _client() as client:
        try:
            resp = await client.get("/streams")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    streams = resp.json().get("streams", [])
    return {
        "streams": [s for s in streams if _owns(current_user, s.get("client_id", ""))]
    }


@router.post("/prime")
async def prime(req: PrimeRequest, current_user: User = Depends(current_active_user)):
    """Arm a one-shot positive capture of ``wakeword`` on a live stream.

    With no ``client_id`` we resolve the caller's active stream (preferring the
    browser recorder), so the Live Record button is a single call. ``wakeword``
    declares which word is being enrolled (clean per-word positive collection).
    """
    if not req.wakeword:
        raise HTTPException(status_code=400, detail="wakeword is required to prime")
    async with _client() as client:
        try:
            client_id = req.client_id
            if client_id is None:
                streams_resp = await client.get("/streams")
                streams_resp.raise_for_status()
                owned = [
                    s.get("client_id", "")
                    for s in streams_resp.json().get("streams", [])
                    if _owns(current_user, s.get("client_id", ""))
                ]
                if not owned:
                    raise HTTPException(
                        status_code=404,
                        detail="No active stream to prime — start recording first.",
                    )
                # Prefer the browser recorder; else the first owned stream.
                client_id = next((c for c in owned if "recorder" in c), owned[0])
            elif not _owns(current_user, client_id):
                raise HTTPException(status_code=403, detail="Not your stream")

            resp = await client.post(
                "/prime", json={"client_id": client_id, "wakeword": req.wakeword}
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/unprime")
async def unprime(req: PrimeRequest, current_user: User = Depends(current_active_user)):
    """Manually end an in-progress prime capture (the Lab 'stop' button).

    Mirrors :func:`prime`'s stream resolution/ownership so the button can call it
    with the same (optional) ``client_id``.
    """
    async with _client() as client:
        try:
            client_id = req.client_id
            if client_id is None:
                streams_resp = await client.get("/streams")
                streams_resp.raise_for_status()
                owned = [
                    s.get("client_id", "")
                    for s in streams_resp.json().get("streams", [])
                    if _owns(current_user, s.get("client_id", ""))
                ]
                if not owned:
                    raise HTTPException(
                        status_code=404, detail="No active stream to stop."
                    )
                client_id = next((c for c in owned if "recorder" in c), owned[0])
            elif not _owns(current_user, client_id):
                raise HTTPException(status_code=403, detail="Not your stream")

            resp = await client.post("/unprime", json={"client_id": client_id})
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/samples")
async def list_samples(
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """List a wake word's captured clips in a bucket, scoped to the caller's streams."""
    async with _client() as client:
        try:
            resp = await client.get(
                "/samples", params={"wakeword": wakeword, "bucket": bucket}
            )
            if resp.status_code == 400:
                raise HTTPException(status_code=400, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    body = resp.json()
    body["samples"] = [
        s
        for s in body.get("samples", [])
        if _owns(current_user, s.get("client_id", ""))
    ]
    return body


@router.get("/samples/stats")
async def sample_stats(current_user: User = Depends(current_active_user)):
    """Per-bucket clip counts for the dashboard."""
    async with _client() as client:
        try:
            resp = await client.get("/samples/stats")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/dedupe")
async def dedupe_samples(
    wakeword: str = Query(...), current_user: User = Depends(current_active_user)
):
    """Remove exact-duplicate clips within a wake word (keeps one per group)."""
    async with _client() as client:
        try:
            resp = await client.post("/samples/dedupe", params={"wakeword": wakeword})
            if resp.status_code == 400:
                raise HTTPException(status_code=400, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/samples/{clip_id}/audio")
async def sample_audio(clip_id: str, current_user: User = Depends(current_active_user)):
    """Stream a clip's WAV bytes for in-browser playback."""
    async with _client() as client:
        try:
            resp = await client.get(f"/samples/{clip_id}/audio")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="clip not found")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return Response(content=resp.content, media_type="audio/wav")


@router.post("/samples/{clip_id}/label")
async def label_sample(
    clip_id: str, req: LabelRequest, current_user: User = Depends(current_active_user)
):
    """Apply a review label (wake/not_wake), moving the clip into positive/negative."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/label", json={"label": req.label}
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/{clip_id}/move")
async def move_sample(
    clip_id: str,
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """Move a clip to a different wake word's bucket (default pending)."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/move",
                params={"wakeword": wakeword, "bucket": bucket},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/{clip_id}/copy")
async def copy_sample(
    clip_id: str,
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """Copy a clip into another wake word's bucket (source stays) — shared FP fan-out."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/copy",
                params={"wakeword": wakeword, "bucket": bucket},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.delete("/samples/{clip_id}")
async def delete_sample(
    clip_id: str, current_user: User = Depends(current_active_user)
):
    """Delete a clip."""
    async with _client() as client:
        try:
            resp = await client.delete(f"/samples/{clip_id}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="clip not found")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()
