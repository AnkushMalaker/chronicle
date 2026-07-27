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

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

from advanced_omi_backend.config import get_diarization_settings
from advanced_omi_backend.constants import BACKGROUND_SPEECH_LABEL, NOISE_LABEL
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient
from advanced_omi_backend.utils.audio_chunk_utils import reconstruct_audio_segment

logger = logging.getLogger(__name__)

# Single-admin assumption, mirroring speaker_recognition_client.
SPEAKER_USER_ID = 1

# compute_cluster_centroids failure reasons that no retry can fix; the backfill
# marks these on the version so the drift report stops offering a pointless backfill.
TERMINAL_CENTROID_FAILURES = {"no_audio", "degenerate_audio"}


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


def _cache_collection():
    return Conversation.get_pymongo_collection().database["drift_report_cache"]


async def drift_fingerprint() -> str:
    """Hash of everything the drift report depends on.

    Covers the enrolled gallery (any enroll/re-enroll/delete bumps a speaker's
    ``updated_at``), each conversation's active version + whether it has centroids
    (reprocess swaps the version id; backfill flips centroid presence), the active
    version's segment count (catches split/merge edits), and the live threshold.
    Same fingerprint → the scan would produce byte-identical results, so it is
    skipped and the cached report served instead.
    """
    client = SpeakerRecognitionClient()
    enrolled = await client.get_enrolled_speakers()
    gallery = sorted(
        (
            str(s.get("id")),
            str(s.get("updated_at")),
            s.get("audio_sample_count"),
        )
        for s in enrolled.get("speakers", [])
    )

    pipeline = [
        {"$match": {"deleted": {"$ne": True}}},
        {
            "$project": {
                "_id": 0,
                "conversation_id": 1,
                "active_transcript_version": 1,
                "versions": {
                    "$map": {
                        "input": {"$ifNull": ["$transcript_versions", []]},
                        "as": "v",
                        "in": {
                            "id": "$$v.version_id",
                            "cent": {
                                "$cond": [
                                    {
                                        "$ifNull": [
                                            "$$v.metadata.cluster_centroids",
                                            False,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                            "unav": {
                                "$cond": [
                                    {
                                        "$ifNull": [
                                            "$$v.metadata.cluster_centroids_unavailable",
                                            False,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                            "nseg": {"$size": {"$ifNull": ["$$v.segments", []]}},
                        },
                    }
                },
            }
        },
    ]
    convs = []
    async for doc in Conversation.get_pymongo_collection().aggregate(pipeline):
        active = next(
            (
                v
                for v in doc["versions"]
                if v["id"] == doc.get("active_transcript_version")
            ),
            None,
        )
        convs.append(
            (
                doc["conversation_id"],
                doc.get("active_transcript_version"),
                active["cent"] if active else 0,
                active["unav"] if active else 0,
                active["nseg"] if active else 0,
            )
        )
    convs.sort()

    payload = json.dumps(
        {"threshold": _threshold(), "gallery": gallery, "convs": convs},
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def get_cached_drift_report() -> Optional[dict]:
    """Return the stored report if the world hasn't changed since it was computed."""
    doc = await _cache_collection().find_one({"_id": "latest"})
    if not doc:
        return None
    if doc.get("fingerprint") != await drift_fingerprint():
        return None
    report = doc["report"]
    report["cached"] = True
    report["computed_at"] = doc.get("computed_at")
    return report


async def store_drift_report(report: dict, fingerprint: str) -> None:
    """Cache the report keyed by the input fingerprint it was computed from."""
    await _cache_collection().replace_one(
        {"_id": "latest"},
        {
            "_id": "latest",
            "fingerprint": fingerprint,
            "report": report,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
        upsert=True,
    )


async def find_drift_conversations(
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
) -> dict:
    """Rank non-deleted conversations by how many speaker labels would change now.

    Cheap: one ``/v1/reidentify-clusters`` call per conversation (no audio, no GPU).
    Conversations without stored centroids are counted under ``no_centroid_data`` (run
    the backfill to cover them); ones the backfill has marked permanently unembeddable
    are counted under ``not_analyzable`` instead so the backfill isn't re-offered.
    ``progress_callback(processed, total, drifted_count)`` fires once per conversation
    when running as a job.
    """
    threshold = _threshold()
    client = SpeakerRecognitionClient()
    convs = await Conversation.find({"deleted": {"$ne": True}}).to_list()

    drifted = []
    scanned = 0
    no_data = 0
    not_analyzable = 0
    total = len(convs)
    for i, conv in enumerate(convs):
        if progress_callback:
            progress_callback(i, total, len(drifted))
        version = conv.active_transcript
        # No speech segments → no speakers → nothing that could drift.
        if not version or not _speech_segments(version):
            continue
        scanned += 1
        meta = version.metadata or {}
        centroids = meta.get("cluster_centroids")
        if not centroids:
            if meta.get("cluster_centroids_unavailable"):
                not_analyzable += 1
            else:
                no_data += 1
            continue

        # Reserved bucket labels (Background Speech / Noise) are not people; their
        # centroids can never re-identify to an enrolled speaker, so scoring them
        # would only manufacture fake "→ Unknown" drift.
        person_centroids = {
            label: vec
            for label, vec in centroids.items()
            if label not in (NOISE_LABEL, BACKGROUND_SPEECH_LABEL)
        }
        if not person_centroids:
            continue

        # Threshold-only acceptance (margin 0, non-exclusive): the stored labels were
        # produced by per-segment identification with NO margin/exclusivity gate, so
        # re-checking them through assign_clusters' stricter default gate reports mass
        # fake drift (e.g. an intact 0.61 ankush match dropped for a 0.52 runner-up).
        resp = await client.reidentify_clusters(
            person_centroids,
            user_id=SPEAKER_USER_ID,
            similarity_threshold=threshold,
            identify_margin=0.0,
            exclusive=False,
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
            # Bucket labels aren't people — nothing to drift.
            if s.speaker in (NOISE_LABEL, BACKGROUND_SPEECH_LABEL):
                continue
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

    if progress_callback:
        progress_callback(total, total, len(drifted))
    drifted.sort(key=lambda c: c["drifted_segments"], reverse=True)
    return {
        "drifted": drifted,
        "total_drifted": len(drifted),
        "conversations_scanned": scanned,
        "no_centroid_data": no_data,
        "not_analyzable": not_analyzable,
        "similarity_threshold": threshold,
    }


async def compute_cluster_centroids(
    conversation_id: str,
    segments,
    client: Optional[SpeakerRecognitionClient] = None,
) -> tuple[dict, Optional[str]]:
    """Embed one pooled centroid per diarized speaker for EXISTING segments.

    Reconstructs the conversation's audio and pools embeddings over the given segment
    boundaries (no re-diarization) via ``/v1/embed-clusters``. Centroid keys are the
    segments' ``speaker`` labels — pass the FINAL display-labelled segments so the
    stored map honors the label contract in the module docstring.

    Returns ``(clusters, failure_reason)``; on failure clusters is ``{}`` — centroids
    power the drift check and are never worth failing a pipeline job over. Reasons
    ``no_audio`` and ``degenerate_audio`` are TERMINAL (retrying can't help); the rest
    are transient.
    """
    diar = [
        {"speaker": s.speaker, "start": float(s.start), "end": float(s.end)}
        for s in segments
        if getattr(s, "segment_type", "speech") == "speech" and s.speaker
    ]
    if not diar:
        return {}, "no_speech_segments"
    client = client or SpeakerRecognitionClient()
    max_end = max(d["end"] for d in diar)
    try:
        audio = await reconstruct_audio_segment(conversation_id, 0.0, max_end + 1.0)
    except Exception as e:
        logger.warning("audio reconstruct failed for %s: %s", conversation_id[:8], e)
        return {}, ("no_audio" if "no audio" in str(e) else "reconstruct_error")
    resp = await client.embed_clusters(audio, diar)
    if resp.get("error"):
        logger.warning(
            "embed_clusters failed for %s: %s",
            conversation_id[:8],
            resp.get("error"),
        )
        return {}, "service_error"
    if not resp.get("clusters"):
        # Service reached, embedder ran, nothing pooled — every segment was
        # silent/degenerate (zero/NaN embeddings). No retry will change that.
        logger.warning(
            "no embeddable speech for %s (all segments degenerate)",
            conversation_id[:8],
        )
        return {}, "degenerate_audio"
    return resp["clusters"], None


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

    done = skipped = failed = marked = 0
    total = len(convs)

    def processed() -> int:
        return done + skipped + failed + marked

    for conv in convs:
        version = conv.active_transcript
        if not version or not version.segments:
            skipped += 1
            if progress_callback:
                progress_callback(processed(), total, done, skipped, failed)
            continue
        meta = version.metadata or {}
        if only_missing and (
            meta.get("cluster_centroids") or meta.get("cluster_centroids_unavailable")
        ):
            skipped += 1
            if progress_callback:
                progress_callback(processed(), total, done, skipped, failed)
            continue

        if not _speech_segments(version):
            skipped += 1
            if progress_callback:
                progress_callback(processed(), total, done, skipped, failed)
            continue

        clusters, fail_reason = await compute_cluster_centroids(
            conv.conversation_id, version.segments, client
        )
        if not clusters:
            if fail_reason in TERMINAL_CENTROID_FAILURES:
                # Permanently unembeddable (no audio stored / all-silent segments).
                # Mark it so the drift report stops offering a backfill that can
                # never succeed — this is what lets the backfill be truly one-time.
                if not version.metadata:
                    version.metadata = {}
                version.metadata["cluster_centroids_unavailable"] = fail_reason
                await conv.save()
                marked += 1
            else:
                failed += 1
            if progress_callback:
                progress_callback(processed(), total, done, skipped, failed)
            continue

        if not version.metadata:
            version.metadata = {}
        version.metadata["cluster_centroids"] = clusters
        version.metadata.pop("cluster_centroids_unavailable", None)
        await conv.save()
        done += 1
        logger.info(
            "backfilled cluster centroids for %s (%d clusters)",
            conv.conversation_id[:8],
            len(clusters),
        )
        if progress_callback:
            progress_callback(processed(), total, done, skipped, failed)
        if limit and done >= limit:
            break

    return {
        "backfilled": done,
        "skipped": skipped,
        "failed": failed,
        "marked_unanalyzable": marked,
    }
