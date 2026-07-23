from advanced_omi_backend.workers.background_suppression import (
    assign_cluster_signatures,
    zone_for,
)


def test_confident_zone_needs_a_clear_margin_over_foreground():
    assert zone_for(0.70, 0.10) == "confident_background"
    # At the similarity floor a wide margin is still enough.
    assert zone_for(0.45, 0.20) == "confident_background"


def test_close_calls_are_queued_not_acted_on():
    # Strong bucket match that doesn't clearly beat the foreground match.
    assert zone_for(0.60, 0.45) == "unsure"
    # Low-similarity band: plausible but the bucket barely knows this source.
    assert zone_for(0.41, 0.25) == "unsure"


def test_weak_or_outmatched_clips_stay_foreground():
    assert zone_for(0.39, 0.10) == "foreground"
    # Margin too thin even with a decent bucket match.
    assert zone_for(0.55, 0.50) == "foreground"
    # A familiar foreground voice wins outright.
    assert zone_for(0.55, 0.60) == "foreground"


def _record(start, text, embedding):
    return {
        "segment_start": start,
        "segment_end": start + 2.0,
        "text": text,
        "embedding": embedding,
    }


def test_same_source_segments_share_a_cluster_signature():
    records = [
        _record(10.0, "game commentary one", [1.0, 0.0]),
        _record(20.0, "game commentary two", [0.99, 0.01]),
        _record(30.0, "a different voice", [0.0, 1.0]),
    ]
    assign_cluster_signatures(records)
    assert records[0]["cluster_signature"] == records[1]["cluster_signature"]
    assert records[0]["cluster_signature"] != records[2]["cluster_signature"]


def test_cluster_signatures_are_stable_across_rescoring():
    embeddings = [[1.0, 0.0], [0.98, 0.02]]
    first = [_record(10.0, "line one", embeddings[0]), _record(20.0, "line two", embeddings[1])]
    second = [_record(10.0, "line one", embeddings[0]), _record(20.0, "line two", embeddings[1])]
    assign_cluster_signatures(first)
    assign_cluster_signatures(second)
    assert first[0]["cluster_signature"] == second[0]["cluster_signature"]
