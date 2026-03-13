"""Embedding generation and management strategies for speaker recognition."""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingStrategy(ABC):
    """Base class for embedding generation strategies."""

    @abstractmethod
    def generate_embedding(
        self, segment_embeddings: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Generate speaker embedding from segment embeddings.

        Args:
            segment_embeddings: List of dicts with 'embedding', 'quality_score', 'duration' etc.

        Returns:
            Combined embedding vector
        """
        pass

    @abstractmethod
    def get_config(self) -> Dict[str, Any]:
        """Get configuration for this strategy."""
        pass


class MeanEmbeddingStrategy(EmbeddingStrategy):
    """Generate embedding by taking mean of all segment embeddings."""

    def generate_embedding(
        self, segment_embeddings: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Calculate mean of all embeddings."""
        if not segment_embeddings:
            raise ValueError("No segment embeddings provided")

        embeddings = []
        for seg in segment_embeddings:
            if isinstance(seg["embedding"], str):
                emb = np.array(json.loads(seg["embedding"]))
            else:
                emb = np.array(seg["embedding"])
            embeddings.append(emb)

        return np.mean(embeddings, axis=0)

    def get_config(self) -> Dict[str, Any]:
        return {"method": "mean"}


class WeightedMeanEmbeddingStrategy(EmbeddingStrategy):
    """Generate embedding by weighted mean based on quality scores."""

    def __init__(self, quality_weight: float = 0.7, duration_weight: float = 0.3):
        self.quality_weight = quality_weight
        self.duration_weight = duration_weight

    def generate_embedding(
        self, segment_embeddings: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Calculate weighted mean of embeddings."""
        if not segment_embeddings:
            raise ValueError("No segment embeddings provided")

        embeddings = []
        weights = []

        for seg in segment_embeddings:
            if isinstance(seg["embedding"], str):
                emb = np.array(json.loads(seg["embedding"]))
            else:
                emb = np.array(seg["embedding"])
            embeddings.append(emb)

            # Calculate weight based on quality and duration
            quality = seg.get("quality_score", 0.5)
            duration = seg.get("duration", 1.0)

            # Normalize duration (assume 2 minutes is ideal)
            duration_norm = min(duration / 120.0, 1.0)

            weight = (
                self.quality_weight * quality + self.duration_weight * duration_norm
            )
            weights.append(weight)

        embeddings = np.array(embeddings)
        weights = np.array(weights)
        weights = weights / weights.sum()  # Normalize weights

        return np.average(embeddings, axis=0, weights=weights)

    def get_config(self) -> Dict[str, Any]:
        return {
            "method": "weighted_mean",
            "quality_weight": self.quality_weight,
            "duration_weight": self.duration_weight,
        }


class BestQualityEmbeddingStrategy(EmbeddingStrategy):
    """Use embedding from the highest quality segment."""

    def generate_embedding(
        self, segment_embeddings: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Select embedding from best quality segment."""
        if not segment_embeddings:
            raise ValueError("No segment embeddings provided")

        # Find segment with highest quality score
        best_segment = max(
            segment_embeddings, key=lambda x: x.get("quality_score", 0.0)
        )

        if isinstance(best_segment["embedding"], str):
            return np.array(json.loads(best_segment["embedding"]))
        else:
            return np.array(best_segment["embedding"])

    def get_config(self) -> Dict[str, Any]:
        return {"method": "best_quality"}


class LongestSegmentEmbeddingStrategy(EmbeddingStrategy):
    """Use embedding from the longest duration segment."""

    def generate_embedding(
        self, segment_embeddings: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Select embedding from longest segment."""
        if not segment_embeddings:
            raise ValueError("No segment embeddings provided")

        # Find segment with longest duration
        best_segment = max(segment_embeddings, key=lambda x: x.get("duration", 0.0))

        if isinstance(best_segment["embedding"], str):
            return np.array(json.loads(best_segment["embedding"]))
        else:
            return np.array(best_segment["embedding"])

    def get_config(self) -> Dict[str, Any]:
        return {"method": "longest_segment"}


class EmbeddingManager:
    """Manages embedding generation strategies."""

    def __init__(self):
        self.strategies = {
            "mean": MeanEmbeddingStrategy(),
            "weighted_mean": WeightedMeanEmbeddingStrategy(),
            "best_quality": BestQualityEmbeddingStrategy(),
            "longest_segment": LongestSegmentEmbeddingStrategy(),
        }

    def get_strategy(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> EmbeddingStrategy:
        """Get embedding strategy by method name.

        Args:
            method: Strategy method name
            params: Optional parameters for strategy initialization

        Returns:
            EmbeddingStrategy instance
        """
        if method not in self.strategies:
            raise ValueError(f"Unknown embedding method: {method}")

        if method == "weighted_mean" and params:
            return WeightedMeanEmbeddingStrategy(**params)

        return self.strategies[method]

    def generate_speaker_embedding(
        self,
        segment_embeddings: List[Dict[str, Any]],
        method: str = "mean",
        params: Optional[Dict[str, Any]] = None,
    ) -> tuple[np.ndarray, Dict[str, Any]]:
        """Generate speaker embedding using specified method.

        Args:
            segment_embeddings: List of segment data with embeddings
            method: Embedding generation method
            params: Optional method parameters

        Returns:
            Tuple of (embedding vector, config dict)
        """
        strategy = self.get_strategy(method, params)
        embedding = strategy.generate_embedding(segment_embeddings)
        config = strategy.get_config()

        logger.info(
            f"Generated speaker embedding using {method} method from {len(segment_embeddings)} segments"
        )

        return embedding, config

    def list_available_methods(self) -> List[str]:
        """Get list of available embedding methods."""
        return list(self.strategies.keys())

    def get_method_description(self, method: str) -> str:
        """Get description of embedding method."""
        descriptions = {
            "mean": "Simple average of all segment embeddings",
            "weighted_mean": "Weighted average based on quality and duration",
            "best_quality": "Use embedding from highest quality segment",
            "longest_segment": "Use embedding from longest duration segment",
        }
        return descriptions.get(method, "Unknown method")
