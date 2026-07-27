"""
OpenAI-compatible API surface.

Exposes an OpenAI-shaped API so external clients (FluidVoice, VoiceInk —
anything that supports a custom OpenAI-compatible endpoint) can use
Chronicle's configured providers:

- POST /api/v1/audio/transcriptions — transcribe through the configured STT
  provider, without creating a conversation.
- POST /api/v1/chat/completions — proxy to the configured LLM (defaults.llm),
  with one retry against defaults.fallback_llm on transport failure.
  Streaming (SSE) passes through.
- GET /api/v1/models — list configured llm/stt models.

Clients use base URL `https://<host>/api/v1` with a Chronicle credential as the
API key (standard `Authorization: Bearer` header). Prefer a long-lived API key
minted at Settings → API Keys — a JWT expires after 24h, which breaks any client
that has no way to log in again.
"""

import io
import logging
import time
import wave
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.model_registry import ModelDef, get_models_registry
from advanced_omi_backend.models.user import User
from advanced_omi_backend.openai_factory import model_supports_temperature
from advanced_omi_backend.services.transcription import (
    RegistryBatchTranscriptionProvider,
    get_transcription_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

# A dead primary must not add its full request timeout to every fallback request.
# This process-local breaker resets quickly so a recovered local service is retried.
_UPSTREAM_FAILURE_COOLDOWN_SECONDS = 60.0
_unavailable_models: dict[str, float] = {}


def _wav_sample_rate(audio_data: bytes) -> int:
    """Read the sample rate from a WAV header, defaulting to 16 kHz."""
    if audio_data[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(audio_data)) as wf:
                return wf.getframerate()
        except wave.Error:
            pass
    return 16000


@router.post("/audio/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    model: Optional[str] = Form(default=None),
    prompt: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    current_user: User = Depends(current_active_user),
):
    """Transcribe an audio file and return only the text (no conversation).

    OpenAI-compatible: multipart `file`, optional `model`/`prompt`/
    `response_format`. If `model` names a configured STT model in the models
    registry it is used; any other value (e.g. the "whisper-1" many clients
    hardcode) falls back to Chronicle's default STT model.
    """
    audio_data = await file.read()
    if not audio_data:
        raise HTTPException(status_code=400, detail="Empty audio file")

    registry = get_models_registry()
    model_def = registry.get_by_name(model) if (registry and model) else None
    if model_def and model_def.model_type == "stt":
        provider = RegistryBatchTranscriptionProvider(model_name=model)
    else:
        provider = get_transcription_provider(mode="batch")
    if provider is None:
        raise HTTPException(
            status_code=503, detail="No STT provider configured in config.yml"
        )

    sample_rate = _wav_sample_rate(audio_data)
    try:
        # priority=True: dictation is latency-sensitive — use the ASR
        # service's priority GPU lane so it never queues behind long batches.
        result = await provider.transcribe(
            audio_data,
            sample_rate,
            diarize=False,
            context_info=prompt or None,
            priority=True,
        )
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))

    text = (result or {}).get("text", "")
    logger.info(
        f"Dictation transcription for {current_user.email} via "
        f"{provider.name}: {len(audio_data)} bytes -> {len(text)} chars"
    )

    if response_format == "text":
        return PlainTextResponse(text)
    return {"text": text}


class _UpstreamTransportError(Exception):
    """Connection failure, timeout, or 5xx from the upstream LLM server."""


def _mark_model_unavailable(model_name: str) -> None:
    _unavailable_models[model_name] = (
        time.monotonic() + _UPSTREAM_FAILURE_COOLDOWN_SECONDS
    )


def _model_is_unavailable(model_name: str) -> bool:
    unavailable_until = _unavailable_models.get(model_name)
    if unavailable_until is None:
        return False
    if unavailable_until > time.monotonic():
        return True
    _unavailable_models.pop(model_name, None)
    return False


def _resolve_chat_model(requested: Optional[str]) -> Optional[ModelDef]:
    """Registry LLM model for the request: an exact registry-name match wins,
    anything else (e.g. the "gpt-4o-mini" a client hardcodes) uses
    defaults.llm."""
    registry = get_models_registry()
    if not registry:
        return None
    if requested:
        named = registry.get_by_name(requested)
        if named and named.model_type == "llm":
            return named
    return registry.get_default("llm")


def _fallback_chat_model(primary: ModelDef) -> Optional[ModelDef]:
    """defaults.fallback_llm, unless unset or identical to the primary."""
    registry = get_models_registry()
    if not registry:
        return None
    fb_name = registry.defaults.get("fallback_llm")
    if not fb_name or fb_name == primary.name:
        return None
    fb = registry.get_by_name(fb_name)
    if not fb or fb.model_type != "llm":
        return None
    return fb


async def _proxy_chat_completion(model_def: ModelDef, body: dict, stream: bool):
    """Forward one chat/completions request to an upstream OpenAI-compatible
    server. Raises _UpstreamTransportError for failures the fallback LLM could
    fix; 4xx responses pass through untouched (config problems)."""
    if _model_is_unavailable(model_def.name):
        raise _UpstreamTransportError(
            f"{model_def.name}: unavailable (cooldown active)"
        )

    url = model_def.resolved_url().rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if model_def.api_key:
        headers["Authorization"] = f"Bearer {model_def.api_key}"
    resolved_model = model_def.model_name or body.get("model")
    payload = {**body, "model": resolved_model}
    # Reasoning models (o1/o3/gpt-5 family) reject non-default temperature and
    # require max_completion_tokens — adapt like the internal LLM client does.
    if not model_supports_temperature(resolved_model):
        payload.pop("temperature", None)
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

    timeout = httpx.Timeout(connect=3.0, read=300.0, write=30.0, pool=3.0)
    if not stream:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            _mark_model_unavailable(model_def.name)
            raise _UpstreamTransportError(f"{model_def.name}: {e}") from e
        if resp.status_code >= 500:
            _mark_model_unavailable(model_def.name)
            raise _UpstreamTransportError(
                f"{model_def.name}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    # Streaming: fallback is only possible before the response starts, so
    # connect/5xx raise; once bytes flow, errors surface to the client.
    client = httpx.AsyncClient(timeout=timeout)
    try:
        request = client.build_request("POST", url, headers=headers, json=payload)
        resp = await client.send(request, stream=True)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        await client.aclose()
        _mark_model_unavailable(model_def.name)
        raise _UpstreamTransportError(f"{model_def.name}: {e}") from e
    if resp.status_code >= 500:
        detail = (await resp.aread())[:200]
        await resp.aclose()
        await client.aclose()
        _mark_model_unavailable(model_def.name)
        raise _UpstreamTransportError(
            f"{model_def.name}: HTTP {resp.status_code} {detail!r}"
        )

    async def relay():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "text/event-stream"),
    )


@router.post("/chat/completions")
async def create_chat_completion(
    request: Request,
    current_user: User = Depends(current_active_user),
):
    """OpenAI-compatible chat completions, proxied to Chronicle's configured LLM.

    The request body is forwarded verbatim except for `model`, which is
    replaced by the resolved registry model's provider-specific name. On
    connection failure/timeout/5xx from the primary, the request is retried
    once against defaults.fallback_llm (non-identical models only).
    """
    body = await request.json()
    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(status_code=400, detail="Request must include 'messages'")

    model_def = _resolve_chat_model(body.get("model"))
    if model_def is None:
        raise HTTPException(status_code=503, detail="No LLM configured in config.yml")

    stream = bool(body.get("stream"))
    try:
        return await _proxy_chat_completion(model_def, body, stream)
    except _UpstreamTransportError as primary_error:
        fallback = _fallback_chat_model(model_def)
        if fallback is None:
            raise HTTPException(status_code=502, detail=str(primary_error))
        logger.warning(
            f"Chat proxy: primary LLM failed ({primary_error}); retrying with "
            f"fallback '{fallback.name}'"
        )
        try:
            return await _proxy_chat_completion(fallback, body, stream)
        except _UpstreamTransportError as fallback_error:
            raise HTTPException(
                status_code=502,
                detail=f"Primary: {primary_error}; fallback: {fallback_error}",
            )


@router.get("/models")
async def list_models(current_user: User = Depends(current_active_user)):
    """OpenAI-compatible model listing: Chronicle's configured llm/stt models,
    identified by their registry names (usable as the `model` field)."""
    registry = get_models_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Models registry unavailable")
    data = [
        {"id": m.name, "object": "model", "owned_by": m.model_provider}
        for model_type in ("llm", "stt")
        for m in registry.get_all_by_type(model_type)
    ]
    return {"object": "list", "data": data}
