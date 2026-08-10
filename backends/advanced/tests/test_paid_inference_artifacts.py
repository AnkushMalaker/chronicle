import gzip
import json

from advanced_omi_backend.services.paid_inference_artifacts import (
    canonical_hash,
    load_reusable_result,
    persist_paid_run,
)


def test_paid_run_persists_complete_stream_and_reuses_structured_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PAID_INFERENCE_ARTIFACT_DIR", str(tmp_path))
    request = {"prompt": "expensive prompt", "model": "gpt-test", "input": [1, 2]}
    result = {"episodes": [{"title": "Work"}], "usage": {"input_tokens": 123}}

    request_hash, artifact_hash = persist_paid_run(
        operation="codex_timeline",
        request=request,
        stdout='{"type":"thread.started"}\n{"type":"turn.completed"}\n',
        stderr="provider diagnostic",
        result=result,
        metadata={"returncode": 0},
        reusable=True,
    )

    assert request_hash == canonical_hash(request)
    artifact = tmp_path / "codex_timeline" / "artifacts" / f"{artifact_hash}.json.gz"
    with gzip.open(artifact, "rt", encoding="utf-8") as stream:
        record = json.load(stream)
    assert record["request"] == request
    assert record["stdout"].splitlines() == [
        '{"type":"thread.started"}',
        '{"type":"turn.completed"}',
    ]
    assert record["stderr"] == "provider diagnostic"
    assert record["result"] == result
    assert load_reusable_result("codex_timeline", request) == result


def test_non_reusable_vault_mutation_has_no_request_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("PAID_INFERENCE_ARTIFACT_DIR", str(tmp_path))
    request = {"prompt": "mutate the current vault", "vault_before_sha256": "abc"}

    persist_paid_run(
        operation="codex_memory",
        request=request,
        stdout="complete event stream",
        stderr="",
        result={"touched": ["People/A.md"]},
        reusable=False,
    )

    assert load_reusable_result("codex_memory", request) is None
    assert list((tmp_path / "codex_memory" / "artifacts").glob("*.json.gz"))
    assert not (tmp_path / "codex_memory" / "requests").exists()
