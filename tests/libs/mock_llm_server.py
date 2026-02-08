#!/usr/bin/env python3
"""
Mock LLM Server - OpenAI-compatible HTTP server for testing.

This server mimics OpenAI's API for chat completions and embeddings without external dependencies.

Architecture:
- HTTP server on 0.0.0.0:11435
- Three endpoints: /v1/chat/completions, /v1/embeddings, /v1/models
- Deterministic responses for reproducible tests

Request Detection:
- Fact extraction: system prompt contains "FACT_RETRIEVAL_PROMPT" or "extract facts"
- Memory updates: system prompt contains "UPDATE_MEMORY_PROMPT" or "memory manager"
"""

import asyncio
import json
import logging
import argparse
import hashlib
from typing import List
from aiohttp import web
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dimensions: int = 1536) -> List[float]:
    """
    Generate deterministic embedding using hash seeding.

    Same text always produces same embedding for reproducible tests.
    Generates unit vector for cosine similarity compatibility.
    """
    # Use SHA-256 hash as seed
    hash_bytes = hashlib.sha256(text.encode('utf-8')).digest()
    seed = int.from_bytes(hash_bytes[:4], 'big')

    # Generate reproducible random vector
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dimensions)

    # Normalize to unit vector (cosine similarity compatible)
    norm = np.linalg.norm(vector)
    return (vector / norm).tolist()


def detect_request_type(messages: List[dict]) -> str:
    """
    Detect request type by analyzing system prompt.

    Returns:
    - "fact_extraction": For fact retrieval prompts
    - "memory_update": For memory manager prompts
    - "general": For other requests
    """
    if not messages:
        return "general"

    # Check first message (usually system prompt)
    first_message = messages[0].get("content", "").lower()

    # Fact extraction detection
    if "fact_retrieval_prompt" in first_message or "extract facts" in first_message:
        return "fact_extraction"

    # Memory update detection
    if "update_memory_prompt" in first_message or "memory manager" in first_message:
        return "memory_update"

    return "general"


def create_fact_extraction_response() -> dict:
    """Create fact extraction response (JSON format)."""
    facts = [
        "User likes hiking",
        "User met with John",
        "Discussed project timeline",
        "User prefers morning meetings",
        "User is working on Chronicle project"
    ]

    content = json.dumps({"facts": facts})

    return {
        "id": "chatcmpl-mock-fact",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150
        }
    }


def create_memory_update_response() -> dict:
    """
    Create memory update response (XML format).

    Supports multiple XML formats:
    - Plain XML: <result>...</result>
    - Markdown code blocks: ```xml ... ```
    - DeepSeek think tags: <think>...</think><result>...</result>
    """
    # Plain XML format (most common)
    xml_content = """<result>
  <memory>
    <item id="0" event="UPDATE">
      <text>User likes hiking in the mountains</text>
      <old_memory>User likes hiking</old_memory>
    </item>
    <item id="1" event="CREATE">
      <text>User prefers morning meetings before 10am</text>
    </item>
  </memory>
</result>"""

    return {
        "id": "chatcmpl-mock-memory",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": xml_content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 80,
            "total_tokens": 230
        }
    }


def create_general_response(user_message: str) -> dict:
    """Create general chat completion response."""
    response_text = f"This is a mock response to: {user_message}"

    return {
        "id": "chatcmpl-mock-general",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 20,
            "total_tokens": 70
        }
    }


async def handle_chat_completions(request: web.Request) -> web.Response:
    """Handle /v1/chat/completions endpoint."""
    try:
        data = await request.json()
        messages = data.get("messages", [])

        # Detect request type
        request_type = detect_request_type(messages)
        logger.info(f"Chat completion request detected as: {request_type}")

        # Generate appropriate response
        if request_type == "fact_extraction":
            response = create_fact_extraction_response()
            logger.info("Returning fact extraction response")

        elif request_type == "memory_update":
            response = create_memory_update_response()
            logger.info("Returning memory update response")

        else:
            user_content = messages[-1].get("content", "") if messages else ""
            response = create_general_response(user_content)
            logger.info("Returning general response")

        return web.json_response(response)

    except Exception as e:
        logger.error(f"Error handling chat completions: {e}", exc_info=True)
        return web.json_response(
            {"error": {"message": str(e), "type": "server_error"}},
            status=500
        )


async def handle_embeddings(request: web.Request) -> web.Response:
    """Handle /v1/embeddings endpoint."""
    try:
        data = await request.json()
        input_texts = data.get("input", [])

        # Ensure input is a list
        if isinstance(input_texts, str):
            input_texts = [input_texts]

        # Generate deterministic embeddings
        embeddings_data = []
        for idx, text in enumerate(input_texts):
            embedding = generate_deterministic_embedding(text, dimensions=1536)
            embeddings_data.append({
                "object": "embedding",
                "embedding": embedding,
                "index": idx
            })

        logger.info(f"Generated {len(embeddings_data)} embeddings")

        response = {
            "object": "list",
            "data": embeddings_data,
            "model": "text-embedding-3-small",
            "usage": {
                "prompt_tokens": len(input_texts) * 10,
                "total_tokens": len(input_texts) * 10
            }
        }

        return web.json_response(response)

    except Exception as e:
        logger.error(f"Error handling embeddings: {e}", exc_info=True)
        return web.json_response(
            {"error": {"message": str(e), "type": "server_error"}},
            status=500
        )


async def handle_models(request: web.Request) -> web.Response:
    """Handle /v1/models endpoint."""
    response = {
        "object": "list",
        "data": [
            {
                "id": "gpt-4o-mini",
                "object": "model",
                "created": 1234567890,
                "owned_by": "mock-llm"
            },
            {
                "id": "text-embedding-3-small",
                "object": "model",
                "created": 1234567890,
                "owned_by": "mock-llm"
            }
        ]
    }

    logger.info("Returning available models")
    return web.json_response(response)


async def handle_health(request: web.Request) -> web.Response:
    """Handle health check endpoint."""
    return web.json_response({"status": "healthy"})


def create_app() -> web.Application:
    """Create aiohttp application with routes."""
    app = web.Application()

    # OpenAI-compatible routes
    app.router.add_post('/v1/chat/completions', handle_chat_completions)
    app.router.add_post('/v1/embeddings', handle_embeddings)
    app.router.add_get('/v1/models', handle_models)

    # Health check
    app.router.add_get('/health', handle_health)

    return app


def main(host: str, port: int):
    """Start HTTP server."""
    logger.info(f"Starting Mock LLM Server on {host}:{port}")
    logger.info(f"OpenAI-compatible endpoints:")
    logger.info(f"  - POST /v1/chat/completions")
    logger.info(f"  - POST /v1/embeddings")
    logger.info(f"  - GET /v1/models")
    logger.info(f"  - GET /health")
    logger.info(f"Deterministic embeddings: 1536 dimensions")

    app = create_app()
    web.run_app(app, host=host, port=port, access_log=logger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock LLM Server")
    parser.add_argument("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=11435, help="Server port (default: 11435)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    try:
        main(args.host, args.port)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
