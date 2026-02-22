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
from urllib.parse import urlencode

import httpx
import websockets

from advanced_omi_backend.config_loader import get_backend_config
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.prompt_registry import get_prompt_registry

from .base import (
    BaseTranscriptionProvider,
    BatchTranscriptionProvider,
    StreamingTranscriptionProvider,
)

logger = logging.getLogger(__name__)


def _parse_hot_words_to_keyterm(hot_words_str: str) -> str:
    """Convert hot words string to Deepgram keyterm format.

    Splits on commas and newlines (context may arrive in either format).

    Input:  "hey vivi\\nchronicle\\nomi"  or  "hey vivi, chronicle, omi"
    Output: "hey vivi Hey Vivi chronicle Chronicle omi Omi"
    """
    if not hot_words_str or not hot_words_str.strip():
        return ""
    import re

    terms = []
    for word in re.split(r"[,\n]+", hot_words_str):
        word = word.strip().lower()
        if not word:
            continue
        terms.append(word)
    return " ".join(terms)


def _dotted_get(d: dict | list | None, dotted: Optional[str]):
    """Safely extract a value from nested dict/list using dotted paths.

    Supports simple dot separators and list indexes like "results[0].alternatives[0].transcript".
    Returns None when the path can't be fully resolved.
    """
    if d is None or not dotted:
        return None
    cur = d
    for part in dotted.split("."):
        if not part:
            continue
        if "[" in part and part.endswith("]"):
            name, idx_str = part[:-1].split("[", 1)
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


def _normalize_provider_segments(segments: list) -> list:
    """Normalize provider-specific segment formats to a standard shape.

    Handles Deepgram paragraph format where:
    - text is nested in ``sentences[].text`` instead of a top-level ``text`` field
    - ``speaker`` is an integer (0, 1) instead of a string ("Speaker 0")

    After normalization every segment dict will have:
    - ``text`` (str): combined sentence text
    - ``speaker`` (str): "Speaker N" label
    - ``start`` / ``end`` (float): time span (preserved from original)
    """
    if not segments:
        return segments

    for seg in segments:
        if not isinstance(seg, dict):
            continue

        # Deepgram paragraphs: text lives inside sentences[], not top-level
        if "text" not in seg and "sentences" in seg:
            sentences = seg.get("sentences", [])
            seg["text"] = " ".join(
                s.get("text", "") for s in sentences if isinstance(s, dict)
            )

        # Normalise integer speaker IDs to "Speaker N" strings
        speaker = seg.get("speaker")
        if isinstance(speaker, (int, float)):
            seg["speaker"] = f"Speaker {int(speaker)}"

    return segments


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
        # Load capabilities from config.yml model definition
        self._capabilities = set(model.capabilities) if model.capabilities else set()

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set:
        """Return provider capabilities from config.yml.

        Capabilities indicate what the provider can produce:
        - word_timestamps: Word-level timing data
        - segments: Speaker segments
        - diarization: Speaker labels in segments

        Returns:
            Set of capability strings
        """
        return self._capabilities

    def get_capabilities_dict(self) -> dict:
        """Return capabilities as a dict for metadata storage.

        Returns:
            Dict mapping capability names to True
        """
        return {cap: True for cap in self._capabilities}

    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int,
        diarize: bool = False,
        context_info: Optional[str] = None,
        progress_callback=None,
        **kwargs,
    ) -> dict:
        # Special handling for mock provider (no HTTP server needed)
        if self.model.model_provider == "mock":
            from .mock_provider import MockTranscriptionProvider

            mock = MockTranscriptionProvider(fail_mode=False)
            return await mock.transcribe(audio_data, sample_rate, diarize)

        op = (self.model.operations or {}).get("stt_transcribe") or {}
        method = (op.get("method") or "POST").upper()
        path = op.get("path") or "/listen"
        # Build URL
        base = self.model.model_url.rstrip("/")
        url = base + ("/" + path.lstrip("/"))

        # Check if we should use multipart file upload (for Parakeet)
        content_type = op.get("content_type", "audio/raw")
        use_multipart = content_type == "multipart/form-data"

        # Build headers (skip Content-Type for multipart as httpx will set it)
        headers = {}
        if not use_multipart:
            # Auto-detect WAV format from RIFF header and use correct Content-Type.
            # Sending WAV data as audio/raw can cause Deepgram to silently return
            # empty transcripts because it tries to decode the WAV header as raw PCM.
            if audio_data[:4] == b"RIFF":
                headers["Content-Type"] = "audio/wav"
            else:
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
                if k.lower() == "authorization" and (
                    not v
                    or v.strip().lower() in ["token", "token ", "bearer", "bearer "]
                ):
                    continue
                headers[k] = v

        # Query params
        query = op.get("query") or {}
        # Inject common params if placeholders used
        if "sample_rate" in query:
            query["sample_rate"] = str(sample_rate)
        if "diarize" in query:
            query["diarize"] = "true" if diarize else "false"

        # Use caller-provided context or fall back to LangFuse prompt store
        if context_info:
            hot_words_str = context_info
        else:
            hot_words_str = ""
            try:
                registry = get_prompt_registry()
                hot_words_str = await registry.get_prompt("asr.hot_words")
            except Exception as e:
                logger.debug(f"Failed to fetch asr.hot_words prompt: {e}")

        # For Deepgram: inject as keyterm query param
        if self.model.model_provider == "deepgram" and hot_words_str.strip():
            keyterm = _parse_hot_words_to_keyterm(hot_words_str)
            if keyterm:
                query["keyterm"] = keyterm

        # NOTE: PULSE (smallest.ai) does NOT support keywords on WebSocket or
        # batch HTTP — any `keywords` query param causes 0 responses or HTTP 400.
        # Hot-word boosting for PULSE is not injected here.

        timeout = op.get("timeout", 300)
        # Use a longer read timeout for NDJSON progress responses — each
        # batch window can take minutes but the service keeps sending
        # progress lines between windows.
        read_timeout = op.get("read_timeout", timeout)
        try:
            timeouts = httpx.Timeout(timeout, read=read_timeout)
            async with httpx.AsyncClient(timeout=timeouts) as client:
                if method == "POST":
                    if use_multipart:
                        # Send as multipart file upload (for Parakeet/VibeVoice)
                        files = {"file": ("audio.wav", audio_data, "audio/wav")}
                        form_data = {}
                        if hot_words_str and hot_words_str.strip():
                            form_data["context_info"] = hot_words_str.strip()

                        # Use streaming to handle NDJSON progress responses
                        async with client.stream(
                            "POST",
                            url,
                            headers=headers,
                            params=query,
                            files=files,
                            data=form_data,
                        ) as resp:
                            resp.raise_for_status()
                            content_type = resp.headers.get("content-type", "")

                            if "application/x-ndjson" in content_type:
                                # Batch progress: read events line by line
                                data = None
                                async for line in resp.aiter_lines():
                                    line = line.strip()
                                    if not line:
                                        continue
                                    event = json.loads(line)
                                    if (
                                        event.get("type") == "progress"
                                        and progress_callback
                                    ):
                                        progress_callback(event)
                                    elif event.get("type") == "result":
                                        data = event
                                if data is None:
                                    raise RuntimeError(
                                        f"NDJSON stream from '{self._name}' ended without a result event"
                                    )
                            else:
                                # Normal JSON response
                                await resp.aread()
                                data = resp.json()
                    else:
                        # Send as raw audio data (for Deepgram)
                        resp = await client.post(
                            url, headers=headers, params=query, content=audio_data
                        )
                        resp.raise_for_status()
                        data = resp.json()
                else:
                    resp = await client.get(url, headers=headers, params=query)
                    resp.raise_for_status()
                    data = resp.json()
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot reach transcription service '{self._name}' at {url}. "
                f"Is the service running? Check that the URL in config.yml "
                f"is correct and the service is accessible from inside Docker "
                f"(use 'host.docker.internal' instead of 'localhost')."
            ) from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise RuntimeError(
                f"Transcription service '{self._name}' at {url} returned HTTP {status}. "
                f"{'Check your API key.' if status in (401, 403) else ''}"
            ) from e

            # DEBUG: Log Deepgram response structure
            if "results" in data and "channels" in data.get("results", {}):
                channels = data["results"]["channels"]
                if channels and "alternatives" in channels[0]:
                    alt = channels[0]["alternatives"][0]
                    logger.debug(
                        f"DEBUG Registry: Deepgram alternative keys: {list(alt.keys())}"
                    )

        # Extract normalized shape
        text, words, segments = "", [], []
        extract = (op.get("response", {}) or {}).get("extract") or {}
        if extract:
            text = _dotted_get(data, extract.get("text")) or ""
            words = _dotted_get(data, extract.get("words")) or []
            segments = _dotted_get(data, extract.get("segments")) or []
            segments = _normalize_provider_segments(segments)

            # Check config to decide whether to keep or discard provider segments
            transcription_config = get_backend_config("transcription")
            use_provider_segments = transcription_config.get(
                "use_provider_segments", False
            )

            if not use_provider_segments:
                segments = []
                logger.debug(
                    f"Transcription: Extracted {len(words)} words, ignoring provider segments (use_provider_segments=false)"
                )
            else:
                logger.debug(
                    f"Transcription: Extracted {len(words)} words, keeping {len(segments)} provider segments (use_provider_segments=true)"
                )

        return {"text": text, "words": words, "segments": segments}


class RegistryStreamingTranscriptionProvider(StreamingTranscriptionProvider):
    """Streaming transcription provider using a config-driven WebSocket template."""

    def __init__(self):
        registry = get_models_registry()
        if not registry:
            raise RuntimeError(
                "config.yml not found; cannot configure streaming STT provider"
            )
        model = registry.get_default("stt_stream")
        if not model:
            raise RuntimeError("No default stt_stream model defined in config.yml")
        self.model = model
        self._name = model.model_provider or model.name
        self._capabilities = set(model.capabilities) if model.capabilities else set()
        self._streams: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set:
        """Return provider capabilities from config.yml."""
        return self._capabilities

    async def transcribe(self, audio_data: bytes, sample_rate: int, **kwargs) -> dict:
        """Not used for streaming providers - use start_stream/process_audio_chunk/end_stream instead."""
        raise NotImplementedError(
            "Streaming providers do not support batch transcription"
        )

    async def start_stream(
        self, client_id: str, sample_rate: int = 16000, diarize: bool = False
    ):
        base_url = self.model.model_url
        ops = self.model.operations or {}

        # Build WebSocket URL with query parameters (for Deepgram streaming)
        query_params = ops.get("query", {})
        query_dict = dict(query_params) if query_params else {}

        # Override sample_rate if provided
        if sample_rate and "sample_rate" in query_dict:
            query_dict["sample_rate"] = sample_rate
        if diarize and "diarize" in query_dict:
            query_dict["diarize"] = "true"

        # Inject hot words for streaming (Deepgram only)
        if self.model.model_provider == "deepgram":
            try:
                registry = get_prompt_registry()
                hot_words_str = await registry.get_prompt("asr.hot_words")
                if hot_words_str and hot_words_str.strip():
                    keyterm = _parse_hot_words_to_keyterm(hot_words_str)
                    if keyterm:
                        query_dict["keyterm"] = keyterm
            except Exception as e:
                logger.debug(f"Failed to fetch asr.hot_words for streaming: {e}")

        # NOTE: PULSE/wave (smallest.ai) does NOT support keywords on WebSocket —
        # any `keywords` query param causes 0 responses or HTTP 400.

        # Normalize boolean values to lowercase strings (Deepgram expects "true"/"false", not "True"/"False")
        normalized_query = {}
        for k, v in query_dict.items():
            if isinstance(v, bool):
                normalized_query[k] = "true" if v else "false"
            else:
                normalized_query[k] = v

        # Build query string with proper URL encoding (NO token in query)
        query_str = urlencode(normalized_query)
        url = f"{base_url}?{query_str}" if query_str else base_url

        # Debug: Log the URL
        logger.info(f"🔗 Connecting to Deepgram WebSocket: {url}")

        # Connect to WebSocket with Authorization header
        headers = {}
        if self.model.api_key:
            auth_prefix = ops.get("auth_prefix") or "Token"
            headers["Authorization"] = f"{auth_prefix} {self.model.api_key}"

        ws = await websockets.connect(url, additional_headers=headers)

        # Send start message if required by provider
        start_msg = (ops.get("start", {}) or {}).get("message", {})
        if start_msg:
            # Inject session_id if placeholder present
            start_msg = json.loads(json.dumps(start_msg))  # deep copy
            start_msg.setdefault("session_id", client_id)
            # Apply sample rate and diarization if present
            if "config" in start_msg and isinstance(start_msg["config"], dict):
                start_msg["config"].setdefault("sample_rate", sample_rate)
                if diarize:
                    start_msg["config"]["diarize"] = True
            await ws.send(json.dumps(start_msg))

            # Wait for confirmation; non-fatal if not provided
            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except Exception:
                pass

        self._streams[client_id] = {
            "ws": ws,
            "sample_rate": sample_rate,
            "final": None,
            "interim": [],
        }

    async def process_audio_chunk(
        self, client_id: str, audio_chunk: bytes
    ) -> dict | None:
        if client_id not in self._streams:
            return None
        ws = self._streams[client_id]["ws"]
        ops = self.model.operations or {}

        # Send chunk header if required (for providers like Parakeet)
        chunk_hdr = (ops.get("chunk_header", {}) or {}).get("message", {})
        if chunk_hdr:
            hdr = json.loads(json.dumps(chunk_hdr))
            hdr.setdefault("type", "audio_chunk")
            hdr.setdefault("session_id", client_id)
            hdr.setdefault("rate", self._streams[client_id]["sample_rate"])
            await ws.send(json.dumps(hdr))

        # Send audio chunk (raw bytes for Deepgram, or after header for others)
        await ws.send(audio_chunk)

        # Non-blocking read for results
        expect = ops.get("expect", {}) or {}
        extract = expect.get("extract", {})
        interim_type = expect.get("interim_type")
        final_type = expect.get("final_type")

        try:
            # Try to read a message (non-blocking)
            msg = await asyncio.wait_for(ws.recv(), timeout=0.05)
            data = json.loads(msg)

            # Determine if this is interim or final result
            is_final = False
            if final_type and data.get("type") == final_type:
                is_final = data.get("is_final", False)
            elif interim_type and data.get("type") == interim_type:
                is_final = data.get("is_final", False)
            else:
                # Fallback: check is_final directly (for providers that don't use a type field)
                is_final = data.get("is_final", False)

            # Extract result data (guard against None from _dotted_get)
            text = (
                _dotted_get(data, extract.get("text"))
                if extract.get("text")
                else data.get("text", "")
            ) or ""
            words = (
                _dotted_get(data, extract.get("words"))
                if extract.get("words")
                else data.get("words", [])
            ) or []
            segments = (
                _dotted_get(data, extract.get("segments"))
                if extract.get("segments")
                else data.get("segments", [])
            ) or []
            segments = _normalize_provider_segments(segments)

            # Calculate confidence if available
            confidence = data.get("confidence", 0.0)
            if not confidence and words and isinstance(words, list):
                # Calculate average word confidence
                confidences = [
                    w.get("confidence", 0.0)
                    for w in words
                    if isinstance(w, dict) and "confidence" in w
                ]
                if confidences:
                    confidence = sum(confidences) / len(confidences)

            # Return result with is_final flag
            # Consumer decides what to do with interim vs final
            return {
                "text": text,
                "words": words,
                "segments": segments,
                "is_final": is_final,
                "confidence": confidence,
            }

        except asyncio.TimeoutError:
            # No message available yet
            return None
        except Exception as e:
            logger.error(f"Error processing audio chunk result for {client_id}: {e}")
            return None

    async def end_stream(self, client_id: str) -> dict:
        if client_id not in self._streams:
            return {"text": "", "words": [], "segments": []}
        ws = self._streams[client_id]["ws"]
        ops = self.model.operations or {}
        end_msg = (ops.get("end", {}) or {}).get("message", {"type": "stop"})
        await ws.send(json.dumps(end_msg))

        expect = ops.get("expect", {}) or {}
        final_type = expect.get("final_type")
        extract = expect.get("extract", {})

        final = None
        try:
            # Drain until final or close
            for _ in range(500):  # hard cap
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                data = json.loads(msg)
                if final_type and data.get("type") == final_type:
                    final = data
                    break
                elif not final_type and (data.get("is_final") or data.get("is_last")):
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
        segments = (
            _dotted_get(final, extract.get("segments"))
            if extract
            else final.get("segments", [])
        ) or []
        return {
            "text": (
                _dotted_get(final, extract.get("text"))
                if extract
                else final.get("text", "")
            ),
            "words": (
                _dotted_get(final, extract.get("words"))
                if extract
                else final.get("words", [])
            ),
            "segments": _normalize_provider_segments(segments),
        }


def get_transcription_provider(
    provider_name: Optional[str] = None, mode: Optional[str] = None
) -> Optional[BaseTranscriptionProvider]:
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


def is_transcription_available(mode: str = "batch") -> bool:
    """Check if transcription provider is available for given mode.

    Args:
        mode: Either "batch" or "streaming"

    Returns:
        True if a transcription provider is configured and available, False otherwise
    """
    provider = get_transcription_provider(mode=mode)
    return provider is not None


def get_mock_transcription_provider(
    fail_mode: bool = False,
) -> BaseTranscriptionProvider:
    """Return a mock transcription provider (for testing only).

    Args:
        fail_mode: If True, transcribe() will raise an exception to simulate transcription failure

    Returns:
        MockTranscriptionProvider instance
    """
    from .mock_provider import MockTranscriptionProvider

    return MockTranscriptionProvider(fail_mode=fail_mode)


__all__ = [
    "get_transcription_provider",
    "is_transcription_available",
    "get_mock_transcription_provider",
    "RegistryBatchTranscriptionProvider",
    "RegistryStreamingTranscriptionProvider",
    "BaseTranscriptionProvider",
    "BatchTranscriptionProvider",
    "StreamingTranscriptionProvider",
]
