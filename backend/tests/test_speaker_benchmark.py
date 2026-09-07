import numpy as np

from backend.workers.speaker_benchmark_jobs import FRACTIONS, _evaluate


def test_learning_curve_uses_disjoint_conversation_folds():
    samples = []
    for speaker, base in (
        ("alex", np.array([1.0, 0.0])),
        ("janhavi", np.array([0.0, 1.0])),
    ):
        for conversation in range(10):
            vector = base + np.array([conversation * 0.001, -conversation * 0.001])
            vector = vector / np.linalg.norm(vector)
            samples.append(
                {
                    "key": f"{speaker}-{conversation}",
                    "speaker": speaker,
                    "conversation_id": f"conversation-{conversation}",
                    "embedding": vector,
                }
            )

    report = _evaluate(samples, threshold=0.5)

    assert [point["fraction"] for point in report["learning_curve"]] == list(FRACTIONS)
    assert report["learning_curve"][-1]["top1_accuracy_mean"] == 1.0
    assigned = [group for groups in report["fold_groups"].values() for group in groups]
    assert sorted(assigned) == sorted({sample["conversation_id"] for sample in samples})
    assert len(assigned) == len(set(assigned))
