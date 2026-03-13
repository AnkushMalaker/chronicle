"""Custom clustering implementation with seeded speaker embeddings."""

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
from pyannote.core import Annotation, Segment
from sklearn.cluster import AgglomerativeClustering as SklearnAgglo
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)


class SeededAgglomerativeClustering:
    """
    Custom clustering implementation that uses known speaker embeddings as seeds
    for improved speaker diarization accuracy.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.15,
        min_cluster_size: int = 3,
        fallback_threshold: float = 0.7,
        confidence_weight: float = 0.8,
    ):
        """
        Initialize seeded clustering with configuration parameters.

        Args:
            similarity_threshold: Minimum cosine similarity to assign to known speaker
            min_cluster_size: Minimum segments required to form a cluster
            fallback_threshold: Threshold for traditional clustering fallback
            confidence_weight: Weight for confidence scoring (0-1)
        """
        self.similarity_threshold = similarity_threshold
        self.min_cluster_size = min_cluster_size
        self.fallback_threshold = fallback_threshold
        self.confidence_weight = confidence_weight

    def cluster_with_seeds(
        self,
        embeddings: np.ndarray,
        known_embeddings: Dict[str, np.ndarray],
        segment_info: List[Dict[str, Any]],
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ) -> Tuple[List[str], List[float], Dict[str, Any]]:
        """
        Perform seeded clustering using known speaker embeddings.

        Args:
            embeddings: Array of segment embeddings (N, embedding_dim)
            known_embeddings: Dict mapping speaker_id to embedding vector
            segment_info: List of segment metadata (start, end, duration, etc.)
            min_speakers: Minimum number of speakers
            max_speakers: Maximum number of speakers

        Returns:
            Tuple of (speaker_labels, confidence_scores, clustering_stats)
        """
        n_segments = len(embeddings)
        if n_segments == 0:
            return (
                [],
                [],
                {
                    "method": "seeded",
                    "num_seeds": len(known_embeddings),
                    "num_assigned_to_seeds": 0,
                    "num_unassigned": 0,
                    "total_segments": 0,
                    "unique_speakers": 0,
                    "similarity_threshold": self.similarity_threshold,
                },
            )

        logger.info(
            f"Starting seeded clustering with {n_segments} segments and {len(known_embeddings)} known speakers"
        )

        # Check for NaN values in input embeddings
        if np.any(np.isnan(embeddings)):
            raise ValueError("Input embeddings contain NaN values")

        for speaker_id, emb in known_embeddings.items():
            if np.any(np.isnan(emb)):
                raise ValueError(
                    f"Known embedding for speaker '{speaker_id}' contains NaN values"
                )

        logger.debug(
            f"Embeddings shape: {embeddings.shape}, embedding range: [{np.min(embeddings):.3f}, {np.max(embeddings):.3f}]"
        )

        # Normalize embeddings for cosine similarity
        embeddings_norm = self._normalize_embeddings(embeddings)
        known_embeddings_norm = {
            speaker_id: self._normalize_embeddings(emb.reshape(1, -1))[0]
            for speaker_id, emb in known_embeddings.items()
        }

        # Check for NaN values after normalization
        if np.any(np.isnan(embeddings_norm)):
            raise ValueError("Normalized embeddings contain NaN values")

        for speaker_id, emb in known_embeddings_norm.items():
            if np.any(np.isnan(emb)):
                raise ValueError(
                    f"Normalized known embedding for speaker '{speaker_id}' contains NaN values"
                )

        # Step 1: Assign segments to known speakers based on similarity
        speaker_labels = ["UNKNOWN"] * n_segments
        confidence_scores = [0.0] * n_segments
        assigned_to_known = 0

        for i, embedding in enumerate(embeddings_norm):
            best_speaker = None
            best_similarity = -1.0

            # Compare with all known speakers
            for speaker_id, known_emb in known_embeddings_norm.items():
                similarity = float(cosine_similarity([embedding], [known_emb])[0, 0])

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_speaker = speaker_id

            # Assign if above threshold
            if best_similarity >= self.similarity_threshold:
                speaker_labels[i] = best_speaker
                confidence_scores[i] = best_similarity
                assigned_to_known += 1
                logger.debug(
                    f"Segment {i}: Assigned to {best_speaker} (similarity: {best_similarity:.3f})"
                )

        # Step 2: Handle unassigned segments with traditional clustering
        unassigned_indices = [
            i for i, label in enumerate(speaker_labels) if label == "UNKNOWN"
        ]

        if unassigned_indices:
            logger.info(
                f"Applying traditional clustering to {len(unassigned_indices)} unassigned segments"
            )
            unassigned_embeddings = embeddings_norm[unassigned_indices]

            # Determine number of clusters for unassigned segments
            num_existing_speakers = len(
                set(label for label in speaker_labels if label != "UNKNOWN")
            )

            if len(unassigned_embeddings) >= self.min_cluster_size:
                # Use traditional agglomerative clustering
                n_clusters = self._estimate_clusters(
                    unassigned_embeddings,
                    min_speakers,
                    max_speakers,
                    num_existing_speakers,
                )

                if n_clusters > 0:
                    clustering = SklearnAgglo(
                        n_clusters=n_clusters, metric="cosine", linkage="average"
                    )
                    cluster_labels = clustering.fit_predict(unassigned_embeddings)

                    # Assign new speaker labels
                    for idx, cluster_id in enumerate(cluster_labels):
                        original_idx = unassigned_indices[idx]
                        new_speaker_id = (
                            f"SPEAKER_{num_existing_speakers + cluster_id:02d}"
                        )
                        speaker_labels[original_idx] = new_speaker_id
                        # Calculate confidence based on cluster cohesion
                        confidence_scores[original_idx] = (
                            self._calculate_cluster_confidence(
                                unassigned_embeddings[idx],
                                unassigned_embeddings,
                                cluster_labels,
                                cluster_id,
                            )
                        )
            else:
                # Assign remaining segments as individual speakers
                for idx_pos, original_idx in enumerate(unassigned_indices):
                    speaker_labels[original_idx] = (
                        f"SPEAKER_{num_existing_speakers + idx_pos:02d}"
                    )
                    confidence_scores[original_idx] = (
                        0.5  # Low confidence for isolated segments
                    )

        # Step 3: Post-process and generate statistics
        clustering_stats = {
            "method": "seeded",
            "num_seeds": len(known_embeddings),
            "num_assigned_to_seeds": assigned_to_known,
            "num_unassigned": len(unassigned_indices),
            "total_segments": n_segments,
            "unique_speakers": len(set(speaker_labels)),
            "similarity_threshold": self.similarity_threshold,
        }

        logger.info(
            f"Seeded clustering complete: {assigned_to_known}/{n_segments} assigned to known speakers, "
            f"{clustering_stats['unique_speakers']} total speakers identified"
        )

        return speaker_labels, confidence_scores, clustering_stats

    def _normalize_embeddings(self, embeddings: np.ndarray) -> np.ndarray:
        """Normalize embeddings to unit length for cosine similarity."""
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        return embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    def _estimate_clusters(
        self,
        embeddings: np.ndarray,
        min_speakers: Optional[int],
        max_speakers: Optional[int],
        num_existing: int,
    ) -> int:
        """
        Estimate optimal number of clusters for unassigned segments.
        """
        n_segments = len(embeddings)

        # Default estimation based on segment count
        estimated = max(1, min(n_segments // self.min_cluster_size, 3))

        # Apply speaker constraints
        if min_speakers is not None:
            estimated = max(estimated, min_speakers - num_existing)

        if max_speakers is not None:
            estimated = min(estimated, max_speakers - num_existing)

        return max(0, estimated)

    def _calculate_cluster_confidence(
        self,
        segment_embedding: np.ndarray,
        all_embeddings: np.ndarray,
        cluster_labels: np.ndarray,
        target_cluster: int,
    ) -> float:
        """
        Calculate confidence score for cluster assignment based on intra-cluster similarity.
        """
        # Find all embeddings in the same cluster
        same_cluster_mask = cluster_labels == target_cluster
        same_cluster_embeddings = all_embeddings[same_cluster_mask]

        if len(same_cluster_embeddings) <= 1:
            return 0.5  # Low confidence for singleton clusters

        # Calculate average similarity to other segments in same cluster
        similarities = cosine_similarity([segment_embedding], same_cluster_embeddings)[
            0
        ]
        # Exclude self-similarity
        other_similarities = similarities[similarities < 0.99]  # Avoid exact matches

        if len(other_similarities) > 0:
            avg_similarity = np.mean(other_similarities)
            # Scale to 0-1 range with some adjustment
            confidence = (avg_similarity + 1) / 2  # Convert from [-1,1] to [0,1]
            return min(0.9, max(0.1, confidence))  # Clamp between 0.1 and 0.9

        return 0.5

    def create_pyannote_annotation(
        self,
        speaker_labels: List[str],
        segment_info: List[Dict[str, Any]],
        confidence_scores: List[float],
    ) -> Annotation:
        """
        Create pyannote Annotation object from clustering results.

        Args:
            speaker_labels: List of speaker labels for each segment
            segment_info: List of segment metadata with start/end times
            confidence_scores: List of confidence scores for each assignment

        Returns:
            pyannote Annotation object
        """
        annotation = Annotation()

        for i, (label, segment, confidence) in enumerate(
            zip(speaker_labels, segment_info, confidence_scores)
        ):
            start_time = segment.get("start", 0)
            end_time = segment.get("end", start_time + segment.get("duration", 0))

            segment_obj = Segment(start_time, end_time)
            annotation[segment_obj] = label

            # Store confidence as metadata if needed
            # Note: pyannote doesn't directly support confidence in Annotation
            # This could be stored separately or in a custom format

        return annotation
