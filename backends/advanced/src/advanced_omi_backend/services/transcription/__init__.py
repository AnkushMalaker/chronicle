"""
Transcription providers and registry-driven factory.

This module exposes a provider that reads its configuration from the
central model registry (config.yml). No environment-based selection
or provider-specific branching is used for batch transcription.
"""

import asyncio
import json
import logging
from typing import Optional

import httpx
import websockets

from advanced_omi_backend.model_registry import get_models_registry
from .base import BaseTranscriptionProvider, BatchTranscriptionProvider, StreamingTranscriptionProvider

logger = logging.getLogger(__name__)


def _dotted_get(d: dict | list | None, dotted: Optional[str]):
    """Safely extract a value from nested dict/list using dotted paths.

    Supports simple dot separators and list indexes like "results[0].alternatives[0].transcript".
    Returns None when the path can't be fully resolved.
    """
    if d is None or not dotted:
        return None
    cur = d
    for part in dotted.split('.'):
        if not part:
            continue
        if '[' in part and part.endswith(']'):
            name, idx_str = part[:-1].split('[', 1)
            if name:
                cur = cur.get(name, {}) if isinstance(cur, dict) else {}
            try:
                idx = int(idx_str)
            except Exception:
                return None
            if isinstance(cur, list) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            cur = cur.get(part, None) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


class RegistryBatchTranscriptionProvider(BatchTranscriptionProvider):
    """Batch transcription provider driven by config.yml."""

    def __init__(self):
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("config.yml not found; cannot configure STT provider")
        model = registry.get_default("stt")
        if not model:
            raise RuntimeError("No default STT model defined in config.yml")
        self.model = model
        self._name = model.model_provider or model.name

    @property
    def name(self) -> str:
        return self._name

    async def transcribe(self, audio_data: bytes, sample_rate: int, diarize: bool = False) -> dict:
        op = (self.model.operations or {}).get("stt_transcribe") or {}
        method = (op.get("method") or "POST").upper()
        path = (op.get("path") or "/listen")
        # Build URL
        base = self.model.model_url.rstrip("/")
        url = base + ("/" + path.lstrip("/"))
        
        # Check if we should use multipart file upload (for Parakeet)
        content_type = op.get("content_type", "audio/raw")
        use_multipart = content_type == "multipart/form-data"
        
        # Build headers (skip Content-Type for multipart as httpx will set it)
        headers = {}
        if not use_multipart:
            headers["Content-Type"] = "audio/raw"
            
        if self.model.api_key:
            # Allow templated header, otherwise fallback to Bearer/Token conventions by config
            hdrs = op.get("headers") or {}
            # Resolve simple ${VAR} placeholders in op headers using env (optional)
            for k, v in hdrs.items():
                if isinstance(v, str):
                    headers[k] = v.replace("${DEEPGRAM_API_KEY:-}", self.model.api_key)
                else:
                    headers[k] = v
        else:
            # When no API key, only add headers that don't require authentication
            hdrs = op.get("headers") or {}
            for k, v in hdrs.items():
                # Skip Authorization headers with empty/invalid values
                if k.lower() == "authorization" and (not v or v.strip().lower() in ["token", "token ", "bearer", "bearer "]):
                    continue
                headers[k] = v

        # Query params
        query = op.get("query") or {}
        # Inject common params if placeholders used
        if "sample_rate" in query:
            query["sample_rate"] = str(sample_rate)
        if "diarize" in query:
            query["diarize"] = "true" if diarize else "false"

        timeout = op.get("timeout", 120)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "POST":
                if use_multipart:
                    # Send as multipart file upload (for Parakeet)
                    files = {"file": ("audio.wav", audio_data, "audio/wav")}
                    resp = await client.post(url, headers=headers, params=query, files=files)
                else:
                    # Send as raw audio data (for Deepgram)
                    resp = await client.post(url, headers=headers, params=query, content=audio_data)
            else:
                resp = await client.get(url, headers=headers, params=query)
            resp.raise_for_status()
            data = resp.json()

            # DEBUG: Log Deepgram response structure
            if "results" in data and "channels" in data.get("results", {}):
                channels = data["results"]["channels"]
                if channels and "alternatives" in channels[0]:
                    alt = channels[0]["alternatives"][0]
                    logger.info(f"DEBUG Registry: Deepgram alternative keys: {list(alt.keys())}")

        # Extract normalized shape
        text, words, segments = "", [], []
        extract = (op.get("response", {}) or {}).get("extract") or {}
        if extract:
            text = _dotted_get(data, extract.get("text")) or ""
            words = _dotted_get(data, extract.get("words")) or []
            segments = _dotted_get(data, extract.get("segments")) or []

            # DEBUG: Log what we extracted
            logger.info(f"DEBUG Registry: Extracted {len(segments)} segments from response")
            if segments and len(segments) > 0:
                logger.info(f"DEBUG Registry: First segment keys: {list(segments[0].keys()) if isinstance(segments[0], dict) else 'not a dict'}")
                logger.info(f"DEBUG Registry: First segment: {segments[0]}")

        return {"text": text, "words": words, "segments": segments}

class RegistryStreamingTranscriptionProvider(StreamingTranscriptionProvider):
    """Streaming transcription provider using a config-driven WebSocket template."""

    def __init__(self):
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("config.yml not found; cannot configure streaming STT provider")
        model = registry.get_default("stt_stream")
        if not model:
            raise RuntimeError("No default stt_stream model defined in config.yml")
        self.model = model
        self._name = model.model_provider or model.name
        self._streams: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return self._name

    async def start_stream(self, client_id: str, sample_rate: int = 16000, diarize: bool = False):
        url = self.model.model_url
        ops = self.model.operations or {}
        start_msg = (ops.get("start", {}) or {}).get("message", {})
        # Inject session_id if placeholder present
        start_msg = json.loads(json.dumps(start_msg))  # deep copy
        start_msg.setdefault("session_id", client_id)
        # Apply sample rate and diarization if present
        if "config" in start_msg and isinstance(start_msg["config"], dict):
            start_msg["config"].setdefault("sample_rate", sample_rate)
            if diarize:
                start_msg["config"]["diarize"] = True

        ws = await websockets.connect(url, open_timeout=10)
        await ws.send(json.dumps(start_msg))
        # Wait for confirmation; non-fatal if not provided
        try:
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        except Exception:
            pass
        self._streams[client_id] = {"ws": ws, "sample_rate": sample_rate, "final": None, "interim": []}

    async def process_audio_chunk(self, client_id: str, audio_chunk: bytes) -> dict | None:
        if client_id not in self._streams:
            return None
        ws = self._streams[client_id]["ws"]
        ops = self.model.operations or {}
        chunk_hdr = (ops.get("chunk_header", {}) or {}).get("message", {})
        hdr = json.loads(json.dumps(chunk_hdr))
        hdr.setdefault("type", "audio_chunk")
        hdr.setdefault("session_id", client_id)
        hdr.setdefault("rate", self._streams[client_id]["sample_rate"])
        await ws.send(json.dumps(hdr))
        await ws.send(audio_chunk)

        # Non-blocking read for interim results
        expect = (ops.get("expect", {}) or {})
        interim_type = expect.get("interim_type")
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.01)
                data = json.loads(msg)
                if interim_type and data.get("type") == interim_type:
                    self._streams[client_id]["interim"].append(data)
        except asyncio.TimeoutError:
            pass
        return None

    async def end_stream(self, client_id: str) -> dict:
        if client_id not in self._streams:
            return {"text": "", "words": [], "segments": []}
        ws = self._streams[client_id]["ws"]
        ops = self.model.operations or {}
        end_msg = (ops.get("end", {}) or {}).get("message", {"type": "stop"})
        await ws.send(json.dumps(end_msg))

        expect = (ops.get("expect", {}) or {})
        final_type = expect.get("final_type")
        extract = expect.get("extract", {})

        final = None
        try:
            # Drain until final or close
            for _ in range(500):  # hard cap
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                data = json.loads(msg)
                if not final_type or data.get("type") == final_type:
                    final = data
                    break
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

        self._streams.pop(client_id, None)

        if not isinstance(final, dict):
            return {"text": "", "words": [], "segments": []}
        return {
            "text": _dotted_get(final, extract.get("text")) if extract else final.get("text", ""),
            "words": _dotted_get(final, extract.get("words")) if extract else final.get("words", []),
            "segments": _dotted_get(final, extract.get("segments")) if extract else final.get("segments", []),
        }


def get_transcription_provider(provider_name: Optional[str] = None, mode: Optional[str] = None) -> Optional[BaseTranscriptionProvider]:
    """Return a registry-driven transcription provider.

    - mode="batch": HTTP-based STT (default)
    - mode="streaming": WebSocket-based STT

    Note: The models registry returns None when config.yml is missing or invalid.
    We avoid broad exception handling here and simply return None when the
    required defaults are not configured.
    """
    registry = get_models_registry()
    if not registry:
        return None

    selected_mode = (mode or "batch").lower()
    if selected_mode == "streaming":
        if not registry.get_default("stt_stream"):
            return None
        return RegistryStreamingTranscriptionProvider()

    # batch mode
    if not registry.get_default("stt"):
        return None
    return RegistryBatchTranscriptionProvider()


__all__ = [
    "get_transcription_provider",
    "RegistryBatchTranscriptionProvider",
    "RegistryStreamingTranscriptionProvider",
    "BaseTranscriptionProvider",
    "BatchTranscriptionProvider",
    "StreamingTranscriptionProvider",
]
