from pathlib import Path


def test_rq_beanie_initialization_registers_annotations():
    """Speaker reprocessing queries annotations from an RQ worker process."""
    source = (
        Path(__file__).parents[1]
        / "src/advanced_omi_backend/models/job.py"
    ).read_text()

    assert "from advanced_omi_backend.models.annotation import Annotation" in source
    assert "document_models=[" in source
    document_models = source.split("document_models=[", 1)[1].split("]", 1)[0]
    assert "Annotation," in document_models
