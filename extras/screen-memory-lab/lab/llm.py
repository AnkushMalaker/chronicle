"""Model access for the lab, with usage accounting and an on-disk cache.

Every prototype in this package is compared on cost as well as accuracy, so all
model traffic goes through here. Responses are cached by a hash of the exact
request, which makes a re-run free and keeps repeated evaluation honest -- the
cache is keyed on the full prompt and images, so any prompt change misses.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

LAB_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(LAB_ROOT / ".env")

CACHE_DIR = Path(
    os.environ.get("SCREEN_MEMORY_LAB_LLM_CACHE", LAB_ROOT / "out" / "llm-cache")
)

# USD per million tokens. Verified against provider pricing pages on 2026-07-25;
# see docs/research/screen-memory/04-prototype-results.md for the citations.
PRICES = {
    "gpt-5.4": (1.25, 10.00),
    "gpt-5.4-mini": (0.25, 2.00),
    "gpt-5.4-nano": (0.05, 0.40),
    "gpt-5.2": (1.25, 10.00),
    "google/gemini-3-flash-preview": (0.30, 2.50),
    "google/gemini-3-pro-preview": (2.00, 12.00),
    # OpenRouter list price 2026-07-26; images bill at the input rate.
    "google/gemini-3.5-flash": (1.50, 9.00),
    "google/gemini-3.5-flash-lite": (0.30, 2.50),
}
UNKNOWN_PRICE = (1.00, 5.00)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0
    seconds: float = 0.0
    by_model: dict = field(default_factory=dict)
    # Callers may drive one client from a thread pool (see vlm_bench/caption_cloud.py),
    # and `+=` on an int is several bytecodes, so counts would silently drift.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, ins: int, outs: int, secs: float, cached: bool) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += ins
            self.output_tokens += outs
            self.seconds += secs
            if cached:
                self.cached_calls += 1
            slot = self.by_model.setdefault(model, {"calls": 0, "in": 0, "out": 0})
            slot["calls"] += 1
            slot["in"] += ins
            slot["out"] += outs

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model, s in self.by_model.items():
            pin, pout = PRICES.get(model, UNKNOWN_PRICE)
            total += s["in"] / 1e6 * pin + s["out"] / 1e6 * pout
        return total

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "model_seconds": round(self.seconds, 1),
            "by_model": self.by_model,
        }


class LLM:
    """A model client for one prototype run."""

    def __init__(
        self, model: str = "gpt-5.4-mini", effort: str = "low", use_cache: bool = True
    ):
        self.model = model
        self.effort = effort
        self.use_cache = use_cache
        self.usage = Usage()
        self._client = None
        self._or_client = None

    # ------------------------------------------------------------- plumbing

    def _openai(self):
        if self._client is None:
            # Imported lazily so the lab runs without the optional openai package.
            from openai import OpenAI

            self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=600.0)
        return self._client

    def _openrouter(self):
        if self._or_client is None:
            # Imported lazily so the lab runs without the optional openai package.
            from openai import OpenAI

            self._or_client = OpenAI(
                api_key=os.environ["OPENROUTER_API_KEY"],
                base_url="https://openrouter.ai/api/v1",
                timeout=600.0,
            )
        return self._or_client

    def _cache_path(self, key: str) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR / f"{key}.json"

    # ---------------------------------------------------------------- calls

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        images: list[Path] | None = None,
        model: str | None = None,
        effort: str | None = None,
        tools: list[dict] | None = None,
        prior: list[dict] | None = None,
        max_output_tokens: int | None = None,
    ) -> dict:
        """One model turn. Returns ``{text, tool_calls, raw_items}``.

        ``prior`` carries previous conversation items for multi-turn tool loops.
        """
        model = model or self.model
        effort = effort or self.effort
        images = images or []

        content: list[dict] = [{"type": "input_text", "text": prompt}]
        img_digest = []
        for path in images:
            data = Path(path).read_bytes()
            img_digest.append(hashlib.sha1(data).hexdigest()[:12])
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{base64.b64encode(data).decode()}",
                }
            )

        items = list(prior or []) + [{"role": "user", "content": content}]

        key_material = json.dumps(
            {
                "model": model,
                "effort": effort,
                "system": system,
                "prompt": prompt,
                "images": img_digest,
                "tools": tools,
                "prior": _strip_images(prior or []),
                "max_out": max_output_tokens,
            },
            sort_keys=True,
            default=str,
        )
        key = hashlib.sha256(key_material.encode()).hexdigest()[:40]
        cache_file = self._cache_path(key)

        if self.use_cache and cache_file.exists():
            payload = json.loads(cache_file.read_text())
            self.usage.add(
                model, payload["input_tokens"], payload["output_tokens"], 0.0, True
            )
            result = payload["result"]
            # Entries cached before the replay fix may still carry output-only
            # fields, which the API rejects on the next turn of a tool loop.
            result["raw_items"] = [_replayable(i) for i in result.get("raw_items", [])]
            return result

        started = time.time()
        if "/" in model:  # OpenRouter model slug
            result, ins, outs = self._call_openrouter(model, system, items, tools)
        else:
            result, ins, outs = self._call_openai(
                model, effort, system, items, tools, max_output_tokens
            )
        elapsed = time.time() - started
        self.usage.add(model, ins, outs, elapsed, False)

        cache_file.write_text(
            json.dumps(
                {
                    "result": result,
                    "input_tokens": ins,
                    "output_tokens": outs,
                    "model": model,
                }
            )
        )
        return result

    def _call_openai(self, model, effort, system, items, tools, max_output_tokens):
        kwargs: dict = {"model": model, "input": items}
        if system:
            kwargs["instructions"] = system
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if tools:
            kwargs["tools"] = tools
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens

        last_error = None
        for attempt in range(4):
            try:
                r = self._openai().responses.create(**kwargs)
                break
            except Exception as exc:  # transient 429/5xx
                last_error = exc
                if attempt == 3:
                    raise
                time.sleep(4 * (attempt + 1))
        else:  # pragma: no cover
            raise last_error  # type: ignore[misc]

        calls = []
        raw_items = []
        for item in r.output:
            dumped = item.model_dump()
            raw_items.append(_replayable(dumped))
            if dumped.get("type") == "function_call":
                calls.append(
                    {
                        "name": dumped["name"],
                        "arguments": dumped.get("arguments") or "{}",
                        "call_id": dumped.get("call_id"),
                    }
                )
        result = {
            "text": r.output_text or "",
            "tool_calls": calls,
            "raw_items": raw_items,
        }
        return result, r.usage.input_tokens, r.usage.output_tokens

    def _call_openrouter(self, model, system, items, tools):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for item in items:
            if item.get("role") != "user":
                continue
            parts = []
            for c in item["content"]:
                if c["type"] == "input_text":
                    parts.append({"type": "text", "text": c["text"]})
                elif c["type"] == "input_image":
                    parts.append(
                        {"type": "image_url", "image_url": {"url": c["image_url"]}}
                    )
            messages.append({"role": "user", "content": parts})
        r = self._openrouter().chat.completions.create(model=model, messages=messages)
        text = r.choices[0].message.content or ""
        usage = r.usage
        return (
            {"text": text, "tool_calls": [], "raw_items": []},
            usage.prompt_tokens,
            usage.completion_tokens,
        )

    # ----------------------------------------------------------------- json

    def json_complete(self, prompt: str, **kwargs) -> dict | list:
        """Complete and parse JSON, tolerating fences and surrounding prose."""
        text = self.complete(prompt, **kwargs)["text"]
        return parse_json(text)


def parse_json(text: str) -> dict | list:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost bracketed span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"model did not return JSON: {text[:400]}")


#

# Fields the API emits on output items but rejects when the same items are sent
# back as input on the next turn of a tool loop.
_OUTPUT_ONLY_KEYS = {"status"}


def _replayable(item):
    """Strip output-only fields so an item can be replayed as conversation input."""
    if isinstance(item, dict):
        return {
            k: _replayable(v)
            for k, v in item.items()
            if k not in _OUTPUT_ONLY_KEYS and v is not None
        }
    if isinstance(item, list):
        return [_replayable(v) for v in item]
    return item


def _strip_images(items: list[dict]) -> list[dict]:
    """Cache-key form of prior items: replace image payloads with their digest."""
    out = []
    for item in items:
        copy = json.loads(json.dumps(item, default=str))
        content = copy.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("image_url"), str):
                    c["image_url"] = hashlib.sha1(c["image_url"].encode()).hexdigest()[
                        :12
                    ]
        out.append(copy)
    return out
