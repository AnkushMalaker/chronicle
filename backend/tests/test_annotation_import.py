"""Regression tests for annotation-dataset ZIP import."""

import io
import json
import zipfile

import pytest

from backend.utils.annotation_import import (
    AnnotationDatasetError,
    parse_annotation_dataset,
)


def _dataset_zip(
    record: dict, *, export_id: str = "annotation_20260628_180119_adaf"
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "export.json",
            json.dumps({"export_id": export_id, "schema_version": 1}),
        )
        archive.writestr("manifest.jsonl", json.dumps(record) + "\n")
        archive.writestr(record["audio_path"], b"RIFF-test-wav")
    return buffer.getvalue()


def _record() -> dict:
    return {
        "clip_id": "conv-abc_000",
        "audio_path": "audio/conv-abc_000.wav",
        "conversation_id": "conv-abc",
        "conversation_title": "Morning chat",
        "client_id": "user01-phone",
        "duration_seconds": 12.5,
        "sample_rate": 16000,
        "text": "machine transcript",
        "segments": [
            {
                "start": 0.2,
                "end": 3.0,
                "speaker": "speaker_0",
                "identified_as": None,
                "text": "machine transcript",
            }
        ],
        "annotation": {
            "text": "human transcript",
            "segments": [
                {
                    "start": 0.2,
                    "end": 3.0,
                    "speaker": "alex",
                    "identified_as": "alex",
                    "text": "human transcript",
                }
            ],
            "notes": "checked",
        },
    }


def test_parse_export_uses_human_annotation_as_active_transcript():
    dataset = parse_annotation_dataset(_dataset_zip(_record()))

    assert dataset.dataset_id == "annotation_20260628_180119_adaf"
    assert len(dataset.clips) == 1
    clip = dataset.clips[0]
    assert clip.transcript == "human transcript"
    assert clip.transcript_source == "human_annotation"
    assert clip.segments[0]["speaker"] == "alex"
    assert clip.audio_bytes == b"RIFF-test-wav"


def test_parse_existing_export_without_schema_version():
    record = _record()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "export.json",
            json.dumps({"export_id": "annotation_20260628_180119_adaf"}),
        )
        archive.writestr("manifest.jsonl", json.dumps(record))
        archive.writestr(record["audio_path"], b"RIFF-test-wav")

    dataset = parse_annotation_dataset(buffer.getvalue())

    assert dataset.schema_version == 1


def test_parse_rejects_audio_path_traversal():
    record = _record()
    record["audio_path"] = "../outside.wav"

    with pytest.raises(AnnotationDatasetError, match="Unsafe"):
        parse_annotation_dataset(_dataset_zip(record))


def test_parse_rejects_manifest_without_audio():
    record = _record()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.jsonl", json.dumps(record))

    with pytest.raises(AnnotationDatasetError, match="missing audio"):
        parse_annotation_dataset(buffer.getvalue())
