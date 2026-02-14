"""
Mock LLM provider for testing without external API dependencies.

This provider returns predefined responses for testing purposes, allowing
tests to run without OpenAI or other external LLM APIs.
"""

import random
from typing import Any, Dict, List, Optional

from ..base import LLMProviderBase


class MockLLMProvider(LLMProviderBase):
    """
    Mock LLM provider for testing.

    Returns predefined memory extractions, embeddings, and action proposals.
    Useful for testing API contracts and data flow without external APIs.
    """

    def __init__(self, config: Dict[str, Any] = None):
        """Initialize the mock LLM provider.

        Args:
            config: Optional configuration dictionary (ignored in mock)
        """
        self._is_connected = False
        self.embedding_dimension = 384  # Standard dimension for mock embeddings

    async def extract_memories(
        self, text: str, prompt: str, user_id: Optional[str] = None,
    ) -> List[str]:
        """
        Return predefined mock memories extracted from text.

        Args:
            text: Input text to extract memories from (analyzed for mock generation)
            prompt: System prompt (ignored in mock)
            user_id: Optional user ID (ignored in mock)

        Returns:
            List of mock memory strings based on text content
        """
        # Generate deterministic mock memories based on text content
        # This simulates what a real LLM would extract

        if not text or len(text.strip()) == 0:
            return []

        # Simple heuristic: generate 1-3 mock memories based on text length
        num_memories = min(3, max(1, len(text) // 200))

        mock_memories = [
            "The user discussed testing without API dependencies.",
            "Mock services are being used for test execution.",
            "The conversation focused on technical implementation details.",
        ]

        # Return subset based on text length
        return mock_memories[:num_memories]

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Generate mock embedding vectors for the given texts.

        Args:
            texts: List of text strings to embed

        Returns:
            List of mock embedding vectors (deterministic based on text content)
        """
        embeddings = []

        for text in texts:
            # Generate deterministic embeddings based on text hash
            # This ensures same text always gets same embedding
            seed = hash(text) % (2**32)
            random.seed(seed)

            # Generate random normalized vector
            embedding = [random.gauss(0, 0.3) for _ in range(self.embedding_dimension)]

            # Normalize to unit length (standard for embeddings)
            magnitude = sum(x**2 for x in embedding) ** 0.5
            normalized_embedding = [x / magnitude for x in embedding]

            embeddings.append(normalized_embedding)

        return embeddings

    async def propose_memory_actions(
        self,
        retrieved_old_memory: List[Dict[str, str]] | List[str],
        new_facts: List[str],
        custom_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return mock memory action proposals.

        Args:
            retrieved_old_memory: List of existing memories (ignored in mock)
            new_facts: List of new facts to process
            custom_prompt: Optional custom prompt (ignored in mock)

        Returns:
            Dictionary containing mock memory actions
        """
        # Return simple ADD actions for all new facts
        # This simulates the LLM deciding to add all new facts as memories

        if not new_facts:
            return {"memory": []}

        actions = []
        for idx, fact in enumerate(new_facts):
            actions.append({
                "id": str(idx),
                "event": "ADD",
                "text": fact,
                "old_memory": None
            })

        return {"memory": actions}

    async def test_connection(self) -> bool:
        """
        Test mock provider connection (always returns True).

        Returns:
            True (mock provider is always available)
        """
        return True
