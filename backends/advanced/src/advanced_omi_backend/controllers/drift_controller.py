"""Identify drift conversations — whose speaker labels would change under the current gallery.

After cleaning voiceprints (Enrollment Health), past conversations may carry stale
speaker identifications ("drift"). This re-identifies each conversation's STORED per-cluster
centroids against the live gallery (pure vector math — no GPU, no re-diarization) and reports
how many segment labels would flip, so you reprocess only the ones that drifted.

Centroids are stored in ``TranscriptVersion.metadata["cluster_centroids"]`` keyed by the
segment's display label (the speaker name or "Unknown Speaker N"), so they map 1:1 to a
version's segments via ``segment.speaker``. Conversations recorded before centroid storage
need the one-time :func:`backfill_cluster_embeddings` pass.
"""

import logging
from collections import Counter
from typing import Callable, Optional

from advanced_omi_backend.config import get_diarization_settings
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

logger = logging.getLogger(__name__)

# Single-admin assumption, mirroring speaker_recognition_client.
SPEAKER_USER_ID = 1


def _speech_segments(version) -> list:
    return [
        s
        for s in (version.segments or [])
        if getattr(s, "segment_type", "speech") == "speech" and s.speaker
    ]


def _threshold() -> Optional[float]:
    try:
        return get_diarization_settings().get("similarity_threshold")
    except Exception:
        return None


async def find_drift_conversations() -> dict:
    """Rank non-deleted conversations by how many speaker labels would change now.

    Cheap: one ``/v1/reidentify-clusters`` call per conversation (no audio, no GPU).
    Conversations without stored centroids are counted under ``no_centroid_data`` (run
    the backfill to cover them).
    """
    threshold = _threshold()
    client = SpeakerRecognitionClient()
    convs = await Conversation.find({"deleted": {"$ne": True}}).to_list()

    drifted = []
    scanned = 0
    no_data = 0
    for conv in convs:
        version = conv.active_transcript
        if not version or not version.segments:
            continue
        scanned += 1
        centroids = (version.metadata or {}).get("cluster_centroids")
        if not centroids:
            no_data += 1
            continue

        resp = await client.reidentify_clusters(
            centroids, user_id=SPEAKER_USER_ID, similarity_threshold=threshold
        )
        if resp.get("error"):
            logger.warning(
                "reidentify failed for %s: %s",
                conv.conversation_id[:8],
                resp.get("error"),
            )
            continue
        assignments = resp.get("assignments", {})

        changed = []
        speech_count = 0
        for s in _speech_segments(version):
            speech_count += 1
            old = s.identified_as or None
            new_a = assignments.get(s.speaker)
            new = (new_a["name"] if new_a else None) or None
            if old != new:
                changed.append((old, new))

        if changed:
            trans = Counter(changed)
            drifted.append(
                {
                    "conversation_id": conv.conversation_id,
                    "title": conv.title or "(untitled)",
                    "speech_segments": speech_count,
                    "drifted_segments": len(changed),
                    "transitions": [
                        {"from": f, "to": t, "count": n}
                        for (f, t), n in trans.most_common()
                    ],
                    "processed_at": (
                        version.created_at.isoformat() if version.created_at else None
                    ),
                }
            )

    drifted.sort(key=lambda c: c["drifted_segments"], reverse=True)
    return {
        "drifted": drifted,
        "total_drifted": len(drifted),
        "conversations_scanned": scanned,
        "no_centroid_data": no_data,
        "similarity_threshold": threshold,
    }


async def backfill_cluster_embeddings(
    limit: Optional[int] = None,
    only_missing: bool = True,
    progress_callback: Optional[Callable[[int, int, int, int, int], None]] = None,
) -> dict:
    """One-time: embed per-cluster centroids for conversations that lack them.

    Reconstructs each conversation's audio and pools one centroid per existing diarized
    speaker (no re-diarization) via ``/v1/embed-clusters``, storing the result keyed by
    the segments' display labels. GPU-bound (runs the embedder); intended to run as a
    background job or from the maintenance script inside the backend container.
    """
    client = SpeakerRecognitionClient()
    convs = await Conversation.find({"deleted": {"$ne": True}}).to_list()

    done = skipped = failed = 0
    total = len(convs)
    for conv in convs:
        version = conv.active_transcript
        if not version or not version.segments:
            skipped += 1
            if progress_callback:
                progress_callback(done + skipped + failed, total, done, skipped, failed)
            continue
        if only_missing and (version.metadata or {}).get("cluster_centroids"):
            skipped += 1
            if progress_callback:
                progress_callback(done + skipped + failed, total, done, skipped, failed)
            continue

        speech = _speech_segments(version)
        diar = [
            {"speaker": s.speaker, "start": float(s.start), "end": float(s.end)}
            for s in speech
        ]
        if not diar:
            skipped += 1
            if progress_callback:
                progress_callback(done + skipped + failed, total, done, skipped, failed)
            continue

        max_end = max(d["end"] for d in diar)
        try:
            audio = await reconstruct_audio_segment(
                conv.conversation_id, 0.0, max_end + 1.0
            )
        except Exception as e:
            logger.warning(
                "audio reconstruct failed for %s: %s", conv.conversation_id[:8], e
            )
            failed += 1
            if progress_callback:
                progress_callback(done + skipped + failed, total, done, skipped, failed)
            continue

        resp = await client.embed_clusters(audio, diar)
        if resp.get("error") or not resp.get("clusters"):
            logger.warning(
                "embed_clusters failed for %s: %s",
                conv.conversation_id[:8],
                resp.get("error") or resp.get("message"),
            )
            failed += 1
            if progress_callback:
                progress_callback(done + skipped + failed, total, done, skipped, failed)
            continue

        if not version.metadata:
            version.metadata = {}
        version.metadata["cluster_centroids"] = resp["clusters"]
        await conv.save()
        done += 1
        logger.info(
            "backfilled cluster centroids for %s (%d clusters)",
            conv.conversation_id[:8],
            len(resp["clusters"]),
        )
        if progress_callback:
            progress_callback(done + skipped + failed, total, done, skipped, failed)
        if limit and done >= limit:
            break

    return {"backfilled": done, "skipped": skipped, "failed": failed}
