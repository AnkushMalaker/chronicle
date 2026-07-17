#!/usr/bin/env python3
"""AI-assisted audio review of a Chronicle annotation dataset (read-only).

Listens to every clip in an exported annotation dataset with an audio-capable
model (Gemini) and flags where the machine transcript disagrees with the audio.
The point is to *review a dataset once before sending it to human annotators* — so
the reviewer sees the likely-wrong spots instead of re-transcribing from scratch.

This tool is deliberately standalone: it reads the export ZIP + writes a sidecar
report. It makes NO database, transcript, or memory changes. Staging the findings
as editor suggestions is a separate, later step (see ``--stage-suggestions`` in the
follow-up design — intentionally not implemented here until output quality is
reviewed).

Why Gemini and not GPT: Gemini can directly analyse audio (transcription,
timing, diarization); OpenAI's text models can only judge transcript coherence,
they cannot listen. See https://ai.google.dev/gemini-api/docs/audio

Usage
-----
Run on 3 clips first, inspect ``ai_audit.jsonl``, then run the whole set:

    uv run --with google-genai python scripts/audit_annotation_dataset.py \
        --dataset annotation_20260628_180119_adaf --limit 3

    uv run --with google-genai python scripts/audit_annotation_dataset.py \
        --dataset annotation_20260628_180119_adaf

The run is resumable: clips already present in the output file are skipped, so a
crash or Ctrl-C mid-run costs nothing. Pass ``--overwrite`` to start fresh.

Requires ``GOOGLE_API_KEY`` (read from the environment, else from
``extras/ml-experiments/.env`` or ``backends/advanced/.env``).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

# scripts/ -> backends/advanced -> repo root
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent.parent
DEFAULT_EXPORTS_DIR = BACKEND_DIR / "data" / "exports"

DEFAULT_MODEL = "gemini-3.1-pro-preview"
OUTPUT_NAME = "ai_audit.jsonl"

# Headroom so a dense clip's thinking + JSON output can't hit the cap and truncate
# the reply into invalid JSON (findings-only output is otherwise small).
MAX_OUTPUT_TOKENS = 65536
UPLOAD_PROCESSING_TIMEOUT_S = 180
REQUEST_TIMEOUT_MS = 300_000
# Retry a clip whose reply doesn't parse — the pro model is occasionally
# nondeterministic about emitting strictly valid JSON.
MAX_ATTEMPTS = 3


ISSUE_TYPES = [
    "mistranscription",  # wrong word(s) — the transcript says X, the audio says Y
    "missing_speech",  # audible speech absent from the transcript
    "hallucinated_text",  # transcript text with no corresponding audio
    "wrong_speaker",  # speech attributed to the wrong speaker
    "punctuation",  # punctuation/casing only; wording is correct
    "script_mix",  # Hindi/English written in the wrong script (romanized vs Devanagari)
    "other",
]

AUDIT_PROMPT = """\
You are auditing an automatic speech-recognition (ASR) transcript against its \
source audio. This transcript will be used to fine-tune an STT model, so \
verbatim accuracy matters more than readability.

You are given:
1. The audio clip.
2. The current machine transcript, split into numbered segments.

Listen to the ENTIRE clip and compare it to each segment. Report every place the \
transcript disagrees with what is actually said. Focus on content errors that \
would teach the model wrong things: wrong words, missed speech, invented text, \
and code-switch script errors. Ignore trivial stylistic choices.

Rules for suggested_text:
* Give the corrected VERBATIM text for that segment (what is actually spoken).
* Keep Hindi in Devanagari and English in Roman/Latin script; do not transliterate.
* Preserve filler words, false starts, repetitions exactly as spoken.
* If a segment is already correct, do NOT include it in findings.

Current transcript segments:
{segments_block}

Respond with a single JSON object of exactly this shape (no markdown, no prose \
outside the JSON):
{{
  "transcript_quality": one of ["good", "minor_issues", "major_issues", "unusable"],
  "overall_notes": "one or two sentences on the clip's overall transcript quality",
  "findings": [
    {{
      "segment_index": <int, the segment number above>,
      "original_text": "<the segment's current text>",
      "suggested_text": "<corrected verbatim text>",
      "issue_type": one of {issue_types},
      "confidence": <float 0.0-1.0, how sure you are the transcript is wrong>,
      "explanation": "<short reason, e.g. 'audio says X not Y'>"
    }}
  ]
}}
If the whole transcript is faithful, return "findings": [].
"""


def _load_api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    # Fall back to the two .env files that may hold it.
    try:
        from dotenv import dotenv_values
    except ImportError:
        dotenv_values = None
    if dotenv_values is not None:
        for env_path in (
            REPO_ROOT / "extras" / "ml-experiments" / ".env",
            BACKEND_DIR / ".env",
        ):
            if env_path.exists():
                val = dotenv_values(env_path).get("GOOGLE_API_KEY")
                if val:
                    return val
    print(
        "ERROR: GOOGLE_API_KEY not found in environment or "
        "extras/ml-experiments/.env / backends/advanced/.env",
        file=sys.stderr,
    )
    sys.exit(2)


def _resolve_dataset_zip(args: argparse.Namespace) -> Path:
    if args.dataset_path:
        p = Path(args.dataset_path)
        if p.is_dir():
            p = p / "dataset.zip"
        if not p.exists():
            print(f"ERROR: dataset ZIP not found: {p}", file=sys.stderr)
            sys.exit(2)
        return p
    exports_dir = Path(args.exports_dir) if args.exports_dir else DEFAULT_EXPORTS_DIR
    zip_path = exports_dir / args.dataset / "dataset.zip"
    if not zip_path.exists():
        print(
            f"ERROR: dataset ZIP not found: {zip_path}\n"
            f"       (looked under exports dir {exports_dir})",
            file=sys.stderr,
        )
        sys.exit(2)
    return zip_path


def _read_manifest(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    records = []
    for line in zf.read("manifest.jsonl").decode("utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _segments_block(record: dict[str, Any]) -> str:
    """Numbered segment listing for the prompt. Falls back to the whole text."""
    segments = record.get("segments") or []
    if not segments:
        return f"[0] (Unknown Speaker) {record.get('text', '').strip()}"
    lines = []
    for i, seg in enumerate(segments):
        speaker = seg.get("identified_as") or seg.get("speaker") or "Unknown Speaker"
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        text = (seg.get("text") or "").strip()
        lines.append(f"[{i}] ({speaker} | {start:.1f}-{end:.1f}s) {text}")
    return "\n".join(lines)


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Parse a model JSON reply, tolerating ```json fences and stray prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    # Grab the outermost {...} if the model wrapped it in prose.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    return json.loads(text)


def _normalize_findings(
    parsed: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    segments = record.get("segments") or []
    findings = []
    for raw in parsed.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        try:
            seg_index = int(raw.get("segment_index", -1))
        except (TypeError, ValueError):
            seg_index = -1
        issue = str(raw.get("issue_type", "other"))
        if issue not in ISSUE_TYPES:
            issue = "other"
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        original = raw.get("original_text")
        if original is None and 0 <= seg_index < len(segments):
            original = segments[seg_index].get("text", "")
        findings.append(
            {
                "segment_index": seg_index,
                "original_text": original or "",
                "suggested_text": raw.get("suggested_text") or "",
                "issue_type": issue,
                "confidence": round(confidence, 3),
                "explanation": (raw.get("explanation") or "").strip(),
            }
        )
    quality = str(parsed.get("transcript_quality", "unknown"))
    if quality not in {"good", "minor_issues", "major_issues", "unusable"}:
        quality = "unknown"
    return {
        "transcript_quality": quality,
        "overall_notes": (parsed.get("overall_notes") or "").strip(),
        "findings": findings,
    }


def _audit_clip(
    client,
    types,
    *,
    model: str,
    record: dict[str, Any],
    audio_bytes: bytes,
    error_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Upload one clip + transcript to Gemini and return the normalized audit."""
    prompt = AUDIT_PROMPT.format(
        segments_block=_segments_block(record),
        issue_types=json.dumps(ISSUE_TYPES),
    )

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    uploaded = None
    try:
        uploaded = client.files.upload(file=tmp_path)
        deadline = time.time() + UPLOAD_PROCESSING_TIMEOUT_S
        while uploaded.state == "PROCESSING":
            if time.time() > deadline:
                raise TimeoutError("Gemini file stuck in PROCESSING")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        if uploaded.state == "FAILED":
            raise RuntimeError("Gemini rejected the audio upload")

        last_error: Exception | None = None
        last_raw = ""
        for _attempt in range(MAX_ATTEMPTS):
            resp = client.models.generate_content(
                model=model,
                contents=[uploaded, prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    response_mime_type="application/json",
                ),
            )
            last_raw = resp.text or ""
            finish = str(resp.candidates[0].finish_reason) if resp.candidates else "?"
            try:
                parsed = _parse_json_response(last_raw)
            except json.JSONDecodeError as exc:
                last_error = RuntimeError(
                    f"unparseable JSON (finish_reason={finish}): {exc}"
                )
                continue
            audit = _normalize_findings(parsed, record)
            usage = resp.usage_metadata
            audit["tokens"] = {
                "input": getattr(usage, "prompt_token_count", 0) or 0,
                "output": getattr(usage, "candidates_token_count", 0) or 0,
                "thinking": getattr(usage, "thoughts_token_count", 0) or 0,
            }
            return audit

        # All attempts failed to parse — persist the last raw reply for inspection
        # rather than throwing it away, then surface the error.
        if error_dir is not None:
            error_dir.mkdir(parents=True, exist_ok=True)
            (error_dir / f"{record['clip_id']}.raw.txt").write_text(last_raw)
        raise last_error or RuntimeError("audit failed")
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _stage_suggestions(args: argparse.Namespace) -> int:
    """Read ai_audit.jsonl and stage its findings as PENDING model suggestions.

    Findings are matched to the *imported* conversations (the memory-excluded
    copies created by Upload → Annotation workspace) via ``external_source_id``
    computed by the very same parser the importer used, so segment indices and
    ids line up exactly. Nothing is applied to a transcript — each finding becomes
    a suggestion the editor renders for accept/edit/reject.
    """
    from beanie import init_beanie

    from advanced_omi_backend.database import db
    from advanced_omi_backend.models.annotation import (
        Annotation,
        AnnotationSource,
        AnnotationStatus,
        AnnotationType,
    )
    from advanced_omi_backend.models.conversation import Conversation
    from advanced_omi_backend.models.user import User
    from advanced_omi_backend.utils.annotation_import import parse_annotation_dataset

    zip_path = _resolve_dataset_zip(args)
    out_path = Path(args.out) if args.out else zip_path.parent / OUTPUT_NAME
    if not out_path.exists():
        print(
            f"ERROR: no audit report at {out_path}. Run the audit (without "
            f"--stage-suggestions) first.",
            file=sys.stderr,
        )
        return 2

    audits: dict[str, dict[str, Any]] = {}
    for line in out_path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            audits[row["clip_id"]] = row

    # Same parser the import endpoint used → identical dataset_id + clip_ids.
    dataset = parse_annotation_dataset(zip_path.read_bytes())

    await init_beanie(database=db, document_models=[User, Conversation, Annotation])

    staged = 0
    skipped_existing = 0
    skipped_conf = 0
    skipped_bounds = 0
    missing_conv = 0
    touched: set[str] = set()

    for clip in dataset.clips:
        row = audits.get(clip.clip_id)
        if not row:
            continue
        external_source_id = f"{dataset.dataset_id}:{clip.clip_id}"
        conv = await Conversation.find_one(
            Conversation.external_source_type == "annotation_dataset",
            Conversation.external_source_id == external_source_id,
        )
        if not conv:
            missing_conv += 1
            continue

        active = conv.active_transcript
        segments = active.segments if active and active.segments else []
        seg_count = len(segments)

        if args.replace:
            await Annotation.find(
                Annotation.conversation_id == conv.conversation_id,
                Annotation.annotation_type == AnnotationType.TRANSCRIPT,
                Annotation.source == AnnotationSource.MODEL_SUGGESTION,
                Annotation.status == AnnotationStatus.PENDING,
            ).delete()

        for finding in row.get("findings", []):
            if float(finding.get("confidence", 0.0)) < args.min_confidence:
                skipped_conf += 1
                continue
            seg_idx = finding.get("segment_index")
            if seg_idx is None or seg_idx < 0 or seg_idx >= seg_count:
                skipped_bounds += 1
                continue

            if not args.replace:
                existing = await Annotation.find_one(
                    Annotation.conversation_id == conv.conversation_id,
                    Annotation.segment_index == seg_idx,
                    Annotation.source == AnnotationSource.MODEL_SUGGESTION,
                    Annotation.status == AnnotationStatus.PENDING,
                )
                if existing:
                    skipped_existing += 1
                    continue

            annotation = Annotation(
                annotation_type=AnnotationType.TRANSCRIPT,
                user_id=conv.user_id,
                conversation_id=conv.conversation_id,
                segment_index=seg_idx,
                # Truthful "before": the segment's current text, matching the
                # convention used when the editor creates transcript annotations.
                original_text=segments[seg_idx].text,
                corrected_text=finding.get("suggested_text", ""),
                source=AnnotationSource.MODEL_SUGGESTION,
                status=AnnotationStatus.PENDING,
            )
            await annotation.save()
            staged += 1
            touched.add(conv.conversation_id)

    print(
        f"Dataset: {dataset.dataset_id}\n"
        f"Staged {staged} suggestion(s) across {len(touched)} conversation(s).\n"
        f"  skipped: {skipped_existing} already-pending, "
        f"{skipped_conf} below --min-confidence={args.min_confidence}, "
        f"{skipped_bounds} out-of-bounds segment_index\n"
        f"  {missing_conv} clip(s) had no imported conversation "
        f"(import the dataset via Upload → Annotation workspace first)\n"
        f"\nReview them at: https://localhost/data-audit?dataset={dataset.dataset_id}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--dataset",
        help="Export id under data/exports/, e.g. annotation_20260628_180119_adaf",
    )
    src.add_argument(
        "--dataset-path",
        help="Explicit path to a dataset.zip (or a directory containing one)",
    )
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only audit the first N not-yet-audited clips (use 3 for a first look)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=f"Output path (default: {OUTPUT_NAME} beside the dataset)",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Ignore existing output and re-audit every clip",
    )
    ap.add_argument(
        "--exports-dir", default=None, help="Override data/exports location"
    )
    ap.add_argument(
        "--stage-suggestions",
        action="store_true",
        help=(
            "Instead of auditing, read an existing ai_audit.jsonl and stage its "
            "findings as PENDING model suggestions on the imported conversations "
            "so they appear in the transcript editor. Requires the dataset to have "
            "been imported (Upload → Annotation workspace) first."
        ),
    )
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="When staging, only stage findings at or above this confidence (0.0-1.0)",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="When staging, clear existing pending model suggestions on each "
        "conversation first (else duplicate segments are skipped)",
    )
    args = ap.parse_args()

    if args.stage_suggestions:
        import asyncio

        return asyncio.run(_stage_suggestions(args))

    api_key = _load_api_key()
    zip_path = _resolve_dataset_zip(args)
    out_path = Path(args.out) if args.out else zip_path.parent / OUTPUT_NAME

    # Resume: collect clip_ids already in the output file.
    done: set[str] = set()
    if out_path.exists() and not args.overwrite:
        for line in out_path.read_text().splitlines():
            if line.strip():
                try:
                    done.add(json.loads(line)["clip_id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    elif args.overwrite and out_path.exists():
        out_path.unlink()

    archive_bytes = zip_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        records = _read_manifest(zf)
        audio_cache = {
            r["clip_id"]: zf.read(r["audio_path"])
            for r in records
            if r.get("audio_path") in zf.namelist()
        }

    pending = [r for r in records if r["clip_id"] not in done]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"Dataset: {zip_path}\n"
        f"Model:   {args.model}\n"
        f"Clips:   {len(records)} total, {len(done)} already audited, "
        f"{len(pending)} to do this run\n"
        f"Output:  {out_path}\n",
        flush=True,
    )
    if not pending:
        print("Nothing to do. (Use --overwrite to re-audit.)")
        return 0

    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )

    ok = 0
    failed = 0
    quality_counts: dict[str, int] = {}
    total_findings = 0
    t_start = time.time()

    with out_path.open("a", encoding="utf-8") as out_f:
        for i, record in enumerate(pending, start=1):
            clip_id = record["clip_id"]
            audio_bytes = audio_cache.get(clip_id)
            label = f"[{i}/{len(pending)}] {clip_id}"
            if not audio_bytes:
                print(f"{label} — SKIP (audio missing in ZIP)", flush=True)
                failed += 1
                continue
            t0 = time.time()
            try:
                audit = _audit_clip(
                    client,
                    types,
                    model=args.model,
                    record=record,
                    audio_bytes=audio_bytes,
                    error_dir=out_path.parent / "ai_audit_errors",
                )
            except Exception as exc:  # noqa: BLE001 - report + continue
                print(f"{label} — ERROR: {exc}", flush=True)
                failed += 1
                continue

            row = {
                "clip_id": clip_id,
                "conversation_id": record.get("conversation_id"),
                "conversation_title": record.get("conversation_title"),
                "audio_path": record.get("audio_path"),
                "duration_seconds": record.get("duration_seconds"),
                "model": args.model,
                **audit,
            }
            out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            out_f.flush()

            ok += 1
            n_find = len(audit["findings"])
            total_findings += n_find
            quality_counts[audit["transcript_quality"]] = (
                quality_counts.get(audit["transcript_quality"], 0) + 1
            )
            print(
                f"{label} — {audit['transcript_quality']}, "
                f"{n_find} finding(s), {time.time() - t0:.1f}s",
                flush=True,
            )

    print(
        f"\nDone in {time.time() - t_start:.0f}s. "
        f"audited={ok} failed={failed} total_findings={total_findings}\n"
        f"quality: {quality_counts}\n"
        f"Report: {out_path}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
