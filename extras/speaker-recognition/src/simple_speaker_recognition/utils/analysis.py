"""Speaker embedding analysis utilities for clustering and visualization."""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.cluster import DBSCAN, KMeans
from sklearn.manifold import TSNE
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import StandardScaler

try:
    import umap.umap_ as umap

    UMAP_AVAILABLE = True
except ImportError:
    UMAP_AVAILABLE = False

log = logging.getLogger(__name__)


def reduce_dimensionality(
    embeddings: np.ndarray,
    method: str = "umap",
    n_components: int = 2,
    random_state: int = 42,
) -> np.ndarray:
    """Reduce dimensionality of embeddings for visualization.

    Args:
        embeddings: Array of shape (n_speakers, embedding_dim)
        method: "umap", "tsne", or "pca"
        n_components: Number of dimensions to reduce to (2 or 3)
        random_state: Random seed for reproducibility

    Returns:
        Reduced embeddings of shape (n_speakers, n_components)
    """
    if len(embeddings) < 2:
        log.warning("Not enough embeddings for dimensionality reduction")
        return np.zeros((len(embeddings), n_components))

    # Normalize embeddings
    embeddings_norm = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    try:
        if method == "umap" and UMAP_AVAILABLE:
            reducer = umap.UMAP(
                n_components=n_components,
                random_state=random_state,
                n_neighbors=min(15, len(embeddings) - 1),
                min_dist=0.1,
            )
            reduced = reducer.fit_transform(embeddings_norm)
        elif method == "tsne":
            # t-SNE works better with pre-scaling
            scaler = StandardScaler()
            embeddings_scaled = scaler.fit_transform(embeddings_norm)

            reducer = TSNE(
                n_components=n_components,
                random_state=random_state,
                perplexity=min(30, len(embeddings) - 1),
                n_iter=1000,
            )
            reduced = reducer.fit_transform(embeddings_scaled)
        else:
            # Fallback to PCA
            from sklearn.decomposition import PCA

            reducer = PCA(n_components=n_components, random_state=random_state)
            reduced = reducer.fit_transform(embeddings_norm)

        log.info(
            f"Reduced {len(embeddings)} embeddings using {method} from {embeddings.shape[1]}D to {n_components}D"
        )
        return reduced

    except Exception as e:
        log.error(f"Error in dimensionality reduction with {method}: {e}")
        # Return zero array as fallback
        return np.zeros((len(embeddings), n_components))


def cluster_speakers(
    embeddings: np.ndarray, method: str = "dbscan", **kwargs
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Cluster speaker embeddings.

    Args:
        embeddings: Array of shape (n_speakers, embedding_dim)
        method: "dbscan" or "kmeans"
        **kwargs: Additional parameters for clustering algorithms

    Returns:
        Tuple of (cluster_labels, cluster_info)
    """
    if len(embeddings) < 2:
        log.warning("Not enough embeddings for clustering")
        return np.zeros(len(embeddings), dtype=int), {"n_clusters": 0, "method": method}

    # Normalize embeddings
    embeddings_norm = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    try:
        if method == "dbscan":
            eps = kwargs.get("eps", 0.3)
            min_samples = kwargs.get("min_samples", 2)

            clusterer = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine")
            labels = clusterer.fit_predict(embeddings_norm)

            n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
            n_noise = list(labels).count(-1)

            cluster_info = {
                "method": "dbscan",
                "n_clusters": n_clusters,
                "n_noise": n_noise,
                "eps": eps,
                "min_samples": min_samples,
            }

        else:  # kmeans
            n_clusters = kwargs.get("n_clusters", min(8, len(embeddings)))
            n_clusters = max(1, min(n_clusters, len(embeddings)))

            clusterer = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = clusterer.fit_predict(embeddings_norm)

            cluster_info = {
                "method": "kmeans",
                "n_clusters": n_clusters,
                "inertia": float(clusterer.inertia_),
            }

        # Calculate silhouette score if we have clusters
        if len(set(labels)) > 1 and len(embeddings) > 1:
            try:
                silhouette = silhouette_score(embeddings_norm, labels)
                cluster_info["silhouette_score"] = float(silhouette)
            except Exception as e:
                log.warning(f"Could not calculate silhouette score: {e}")
                cluster_info["silhouette_score"] = None
        else:
            cluster_info["silhouette_score"] = None

        log.info(
            f"Clustered {len(embeddings)} embeddings using {method}: {cluster_info}"
        )
        return labels, cluster_info

    except Exception as e:
        log.error(f"Error in clustering with {method}: {e}")
        return np.zeros(len(embeddings), dtype=int), {
            "n_clusters": 0,
            "method": method,
            "error": str(e),
        }


def find_similar_speakers(
    embeddings: np.ndarray, speaker_names: List[str], threshold: float = 0.8
) -> List[Dict[str, Any]]:
    """Find pairs of similar speakers based on embedding similarity.

    Args:
        embeddings: Array of shape (n_speakers, embedding_dim)
        speaker_names: List of speaker names
        threshold: Cosine similarity threshold for considering speakers similar

    Returns:
        List of similar speaker pairs with similarity scores
    """
    if len(embeddings) < 2:
        return []

    # Normalize embeddings
    embeddings_norm = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    # Calculate cosine similarity matrix
    similarity_matrix = cosine_similarity(embeddings_norm)

    similar_pairs = []

    # Find pairs above threshold (excluding self-similarity)
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            similarity = similarity_matrix[i, j]

            if similarity >= threshold:
                similar_pairs.append(
                    {
                        "speaker1": speaker_names[i],
                        "speaker2": speaker_names[j],
                        "similarity": float(similarity),
                        "speaker1_idx": i,
                        "speaker2_idx": j,
                    }
                )

    # Sort by similarity descending
    similar_pairs.sort(key=lambda x: x["similarity"], reverse=True)

    log.info(
        f"Found {len(similar_pairs)} similar speaker pairs above threshold {threshold}"
    )
    return similar_pairs


def analyze_embedding_quality(embeddings: np.ndarray) -> Dict[str, Any]:
    """Analyze the quality and distribution of embeddings.

    Args:
        embeddings: Array of shape (n_speakers, embedding_dim)

    Returns:
        Dictionary with quality metrics
    """
    if len(embeddings) == 0:
        return {"error": "No embeddings provided"}

    try:
        # Basic statistics
        mean_norm = np.mean(np.linalg.norm(embeddings, axis=1))
        std_norm = np.std(np.linalg.norm(embeddings, axis=1))

        # Pairwise distances
        if len(embeddings) > 1:
            embeddings_norm = embeddings / np.linalg.norm(
                embeddings, axis=1, keepdims=True
            )
            similarity_matrix = cosine_similarity(embeddings_norm)

            # Get upper triangle (excluding diagonal)
            upper_tri_mask = np.triu(np.ones_like(similarity_matrix, dtype=bool), k=1)
            similarities = similarity_matrix[upper_tri_mask]

            mean_similarity = np.mean(similarities)
            std_similarity = np.std(similarities)
            min_similarity = np.min(similarities)
            max_similarity = np.max(similarities)
        else:
            mean_similarity = std_similarity = min_similarity = max_similarity = 0.0

        quality_metrics = {
            "n_speakers": len(embeddings),
            "embedding_dim": embeddings.shape[1],
            "mean_norm": float(mean_norm),
            "std_norm": float(std_norm),
            "mean_similarity": float(mean_similarity),
            "std_similarity": float(std_similarity),
            "min_similarity": float(min_similarity),
            "max_similarity": float(max_similarity),
            "separation_quality": (
                float(1.0 - mean_similarity) if len(embeddings) > 1 else 1.0
            ),
        }

        log.info(f"Analyzed embedding quality for {len(embeddings)} speakers")
        return quality_metrics

    except Exception as e:
        log.error(f"Error analyzing embedding quality: {e}")
        return {"error": str(e)}


def create_speaker_analysis(
    embeddings_dict: Dict[str, np.ndarray],
    method: str = "umap",
    cluster_method: str = "dbscan",
    similarity_threshold: float = 0.8,
    speaker_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Create comprehensive analysis of speaker embeddings.

    Args:
        embeddings_dict: Dictionary mapping speaker_id -> embedding
        method: Dimensionality reduction method
        cluster_method: Clustering method
        similarity_threshold: Threshold for finding similar speakers
        speaker_names: Optional dictionary mapping speaker_id -> speaker_name

    Returns:
        Complete analysis results
    """
    if not embeddings_dict:
        return {"error": "No embeddings provided"}

    try:
        # Extract embeddings and metadata
        speaker_ids = list(embeddings_dict.keys())
        embeddings = np.array(list(embeddings_dict.values()))

        log.info(
            f"Analyzing {len(speaker_ids)} speakers with {embeddings.shape[1]}D embeddings"
        )

        # Handle single embedding case specially
        if len(embeddings) == 1:
            log.info("Single embedding detected - using simplified analysis")
            # For single embedding, create minimal valid response
            return {
                "visualization": {
                    "speakers": speaker_ids,
                    "embeddings_2d": [[0.0, 0.0]],  # Center point
                    "embeddings_3d": [[0.0, 0.0, 0.0]],  # Center point
                    "cluster_labels": [0],  # Single cluster
                    "colors": ["hsl(180, 70%, 50%)"],  # Single color
                },
                "clustering": {
                    "method": cluster_method,
                    "n_clusters": 1,
                    "silhouette_score": None,
                    "n_noise": 0,
                },
                "similar_speakers": [],  # No pairs with single speaker
                "quality_metrics": {
                    "n_speakers": 1,
                    "mean_similarity": None,
                    "std_similarity": None,
                    "separation_quality": None,
                },
                "parameters": {
                    "reduction_method": method,
                    "cluster_method": cluster_method,
                    "similarity_threshold": similarity_threshold,
                },
            }

        # Dimensionality reduction for visualization
        reduced_2d = reduce_dimensionality(embeddings, method=method, n_components=2)
        reduced_3d = reduce_dimensionality(embeddings, method=method, n_components=3)

        # Clustering
        cluster_labels, cluster_info = cluster_speakers(
            embeddings, method=cluster_method
        )

        # Similar speakers - use names if available
        speaker_display_names = []
        for spk_id in speaker_ids:
            if speaker_names and spk_id in speaker_names:
                speaker_display_names.append(speaker_names[spk_id])
            else:
                speaker_display_names.append(spk_id)

        similar_pairs = find_similar_speakers(
            embeddings, speaker_display_names, similarity_threshold
        )

        # Quality analysis
        quality_metrics = analyze_embedding_quality(embeddings)

        # Prepare visualization data
        visualization_data = {
            "speakers": speaker_ids,
            "embeddings_2d": reduced_2d.tolist(),
            "embeddings_3d": reduced_3d.tolist(),
            "cluster_labels": cluster_labels.tolist(),
            "colors": [
                f"hsl({(hash(spk_id) % 360)}, 70%, 50%)" for spk_id in speaker_ids
            ],
        }

        analysis_result = {
            "visualization": visualization_data,
            "clustering": cluster_info,
            "similar_speakers": similar_pairs,
            "quality_metrics": quality_metrics,
            "parameters": {
                "reduction_method": method,
                "cluster_method": cluster_method,
                "similarity_threshold": similarity_threshold,
            },
            "status": "success",
        }

        log.info("Speaker analysis completed successfully")
        return analysis_result

    except Exception as e:
        log.error(f"Error in speaker analysis: {e}")
        return {"error": str(e), "status": "failed"}
