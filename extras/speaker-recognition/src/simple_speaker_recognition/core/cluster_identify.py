"""Assign diarized-speaker cluster centroids to enrolled speakers.

Factored out of the diarize-identify-match endpoint so the exact same greedy +
margin + exclusive assignment can be replayed offline. That replay powers the
"reprocess impact" check: given a past conversation's stored per-cluster centroids,
re-run identification against the *current* gallery (no GPU, no re-diarization) and
see whether any speaker labels would change after voiceprint cleanup.
"""

import logging
from typing import Dict, List

import numpy as np

log = logging.getLogger("speaker_service")


async def assign_clusters_to_speakers(
    db,
    label_centroids: Dict[str, np.ndarray],
    user_id: int,
    similarity_threshold: float,
    identify_margin: float = 0.1,
    exclusive: bool = True,
) -> Dict[str, Dict]:
    """Greedily map each diarized-speaker centroid to an enrolled speaker.

    Most-confident cluster first; applies the open-set threshold, a best-vs-runner-up
    margin, and (optionally) exclusivity so one enrolled person can't claim two diarized
    speakers. Returns ``{cluster_label: {name, id, confidence}}`` for confidently
    identified clusters (unidentified clusters are simply absent).
    """
    cluster_candidates: Dict[str, List[Dict]] = {}
    for label, centroid in label_centroids.items():
        _, _, _, ranked = await db.identify_with_candidates(
            np.asarray(centroid, dtype=np.float32), user_id=user_id
        )
        cluster_candidates[label] = ranked

    label_assignment: Dict[str, Dict] = {}
    taken_speaker_ids: set = set()
    for label in sorted(
        cluster_candidates,
        key=lambda lbl: (
            cluster_candidates[lbl][0]["similarity"]
            if cluster_candidates[lbl]
            else -1.0
        ),
        reverse=True,
    ):
        ranked = cluster_candidates[label]
        available = [
            c
            for c in ranked
            if c["similarity"] >= similarity_threshold
            and (not exclusive or c["id"] not in taken_speaker_ids)
        ]
        if not available:
            continue
        chosen = available[0]
        runner_up = available[1]["similarity"] if len(available) > 1 else 0.0
        if chosen["similarity"] - runner_up < identify_margin and len(available) > 1:
            log.info(
                f"Cluster {label}: ambiguous "
                f"({chosen['name']}={chosen['similarity']:.3f} vs runner-up={runner_up:.3f}, "
                f"margin<{identify_margin}) -> unknown"
            )
            continue
        label_assignment[label] = {
            "name": chosen["name"],
            "id": chosen["id"],
            "confidence": chosen["similarity"],
        }
        if exclusive:
            taken_speaker_ids.add(chosen["id"])
    return label_assignment
