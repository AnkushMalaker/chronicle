from pathlib import Path

import pytest

from backend.models import job as job_module


def test_rq_beanie_initialization_registers_annotations():
    """Speaker reprocessing queries annotations from an RQ worker process."""
    source = (Path(__file__).parents[1] / "src/backend/models/job.py").read_text()

    assert "from backend.models.annotation import Annotation" in source
    assert "document_models=[" in source
    document_models = source.split("document_models=[", 1)[1].split("]", 1)[0]
    assert "Annotation," in document_models


def test_async_job_flushes_otel_after_success(monkeypatch):
    flushed = []
    monkeypatch.setattr(job_module, "force_flush_otel", lambda: flushed.append(True))

    @job_module.async_job(redis=False, beanie=False)
    async def sample_job():
        return "done"

    assert sample_job() == "done"
    assert flushed == [True]


def test_async_job_flushes_otel_after_failure(monkeypatch):
    flushed = []
    monkeypatch.setattr(job_module, "force_flush_otel", lambda: flushed.append(True))

    @job_module.async_job(redis=False, beanie=False)
    async def sample_job():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sample_job()
    assert flushed == [True]
