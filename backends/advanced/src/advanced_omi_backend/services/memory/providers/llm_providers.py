"""LLM provider implementations for memory service.

This module provides concrete implementations of LLM providers for:
- OpenAI (GPT models)
- Ollama (local models)

Each provider handles memory extraction, embedding generation, and
memory action proposals using their respective APIs.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..base import LLMProviderBase
from ..prompts import (
    FACT_RETRIEVAL_PROMPT,
    build_update_memory_messages,
    get_update_memory_messages,
)
from ..update_memory_utils import (
    extract_assistant_xml_from_openai_response,
    items_to_json,
    parse_memory_xml,
)
from ..utils import extract_json_from_text

# TODO: Re-enable spacy when Docker build is fixed
# import spacy


memory_logger = logging.getLogger("memory_service")

# New: config-driven model registry + universal client
from advanced_omi_backend.model_registry import ModelDef, get_models_registry


def _is_langfuse_enabled() -> bool:
    """Check if Langfuse is properly configured."""
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
        and os.getenv("LANGFUSE_HOST")
    )


def _get_openai_client(api_key: str, base_url: str, is_async: bool = False):
    """Get OpenAI client with optional Langfuse tracing.

    Args:
        api_key: OpenAI API key
        base_url: OpenAI API base URL
        is_async: Whether to return async or sync client

    Returns:
        OpenAI client instance (with or without Langfuse tracing)
    """
    if _is_langfuse_enabled():
        # Use Langfuse-wrapped OpenAI for tracing
        import langfuse.openai as openai
        memory_logger.debug("Using OpenAI client with Langfuse tracing")
    else:
        # Use regular OpenAI client without tracing
        from openai import AsyncOpenAI, OpenAI
        openai = type('OpenAI', (), {'OpenAI': OpenAI, 'AsyncOpenAI': AsyncOpenAI})()
        memory_logger.debug("Using OpenAI client without tracing")

    if is_async:
        return openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        return openai.OpenAI(api_key=api_key, base_url=base_url)


async def generate_openai_embeddings(
    texts: List[str],
    api_key: str,
    base_url: str,
    model: str,
) -> List[List[float]]:
    """Generate embeddings with the async OpenAI client."""
    client = _get_openai_client(
        api_key=api_key,
        base_url=base_url,
        is_async=True,
    )
    response = await client.embeddings.create(
        model=model,
        input=texts,
    )
    return [data.embedding for data in response.data]

# TODO: Re-enable spacy when Docker build is fixed
# try:
#     nlp = spacy.load("en_core_web_sm")
# except OSError:
#     # Model not installed, fallback to None
#     memory_logger.warning("spacy model 'en_core_web_sm' not found. Using fallback text chunking.")
#     nlp = None
nlp = None  # Temporarily disabled

def chunk_text_with_spacy(text: str, max_tokens: int = 100) -> List[str]:
    """Split text into chunks using spaCy sentence segmentation.
    max_tokens is the maximum number of words in a chunk.
    """
    # Fallback chunking when spacy is not available
    if nlp is None:
        # Simple sentence-based chunking
        sentences = text.replace('\n', ' ').split('. ')
        chunks = []
        current_chunk = ""
        current_tokens = 0
        
        for sentence in sentences:
            sentence_tokens = len(sentence.split())
            
            if current_tokens + sentence_tokens > max_tokens and current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = sentence
                current_tokens = sentence_tokens
            else:
                if current_chunk:
                    current_chunk += ". " + sentence
                else:
                    current_chunk = sentence
                current_tokens += sentence_tokens
        
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        
        return chunks if chunks else [text]
    
    # Original spacy implementation when available
    doc = nlp(text)
    
    chunks = []
    current_chunk = ""
    current_tokens = 0
    
    for sent in doc.sents:
        sent_text = sent.text.strip()
        sent_tokens = len(sent_text.split())  # Simple word count
        
        if current_tokens + sent_tokens > max_tokens and current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = sent_text
            current_tokens = sent_tokens
        else:
            current_chunk += " " + sent_text if current_chunk else sent_text
            current_tokens += sent_tokens
    
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    
    return chunks

class OpenAIProvider(LLMProviderBase):
    """Config-driven LLM provider using OpenAI SDK (OpenAI-compatible).

    Uses the official OpenAI client (with custom base_url and api_key) to call
    chat and embeddings across OpenAI-compatible providers (OpenAI, Ollama, Groq).
    Models and endpoints are resolved from config.yml via the model registry.
    """

    def __init__(self, config: Dict[str, Any]):
        # Ignore provider-specific envs; use registry as single source of truth
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("config.yml not found or invalid; cannot initialize model registry")

        # Resolve default models
        self.llm_def: ModelDef = registry.get_default("llm")  # type: ignore
        self.embed_def: ModelDef | None = registry.get_default("embedding")

        if not self.llm_def:
            raise RuntimeError("No default LLM defined in config.yml")

        # Store parameters for LLM
        self.api_key = self.llm_def.api_key or ""
        self.base_url = self.llm_def.model_url
        self.model = self.llm_def.model_name
        self.temperature = float(self.llm_def.model_params.get("temperature", 0.1))
        self.max_tokens = int(self.llm_def.model_params.get("max_tokens", 2000))

        # Store parameters for embeddings (use separate config if available)
        self.embedding_model = (self.embed_def.model_name if self.embed_def else self.llm_def.model_name)
        self.embedding_api_key = (self.embed_def.api_key if self.embed_def else self.api_key)
        self.embedding_base_url = (self.embed_def.model_url if self.embed_def else self.base_url)

        # CRITICAL: Validate API keys are present - fail fast instead of hanging
        if not self.api_key or self.api_key.strip() == "":
            raise RuntimeError(
                f"API key is missing or empty for LLM provider '{self.llm_def.model_provider}' (model: {self.model}). "
                f"Please set the API key in config.yml or environment variables. "
                f"Cannot proceed without valid API credentials."
            )

        if self.embed_def and (not self.embedding_api_key or self.embedding_api_key.strip() == ""):
            raise RuntimeError(
                f"API key is missing or empty for embedding provider '{self.embed_def.model_provider}' (model: {self.embedding_model}). "
                f"Please set the API key in config.yml or environment variables."
            )

        # Lazy client creation
        self._client = None

    async def extract_memories(self, text: str, prompt: str) -> List[str]:
        """Extract memories using OpenAI API with the enhanced fact retrieval prompt.

        Args:
            text: Input text to extract memories from
            prompt: System prompt to guide extraction (uses default if empty)

        Returns:
            List of extracted memory strings
        """
        try:
            # Use the provided prompt or fall back to default
            system_prompt = prompt if prompt.strip() else FACT_RETRIEVAL_PROMPT
            
            # local models can only handle small chunks of input text
            text_chunks = chunk_text_with_spacy(text)
            
            # Process all chunks in sequence, not concurrently
            results = [await self._process_chunk(system_prompt, chunk, i) for i, chunk in enumerate(text_chunks)]
            
            # Spread list of list of facts into a single list of facts
            cleaned_facts = []
            for result in results:
                memory_logger.info(f"Cleaned facts: {result}")
                cleaned_facts.extend(result)
            
            return cleaned_facts
                
        except Exception as e:
            memory_logger.error(f"OpenAI memory extraction failed: {e}")
            return []
        
    async def _process_chunk(self, system_prompt: str, chunk: str, index: int) -> List[str]:
        """Process a single text chunk to extract memories using OpenAI API.
        
        This private method handles the LLM interaction for a single chunk of text,
        sending it to OpenAI's chat completion API with the specified system prompt
        to extract structured memory facts.
        
        Args:
            client: OpenAI async client instance for API communication
            system_prompt: System prompt that guides the memory extraction behavior
            chunk: Individual text chunk to process for memory extraction
            index: Index of the chunk for logging and error tracking purposes
            
        Returns:
            List of extracted memory fact strings from the chunk. Returns empty list
            if no facts are found or if an error occurs during processing.
            
        Note:
            Errors are logged but don't propagate to avoid failing the entire
            memory extraction process.
        """
        try:
            client = _get_openai_client(api_key=self.api_key, base_url=self.base_url, is_async=True)
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
            facts = (response.choices[0].message.content or "").strip()
            if not facts:
                return []

            return _parse_memories_content(facts)
            
        except Exception as e:
            memory_logger.error(f"Error processing chunk {index}: {e}")
            return []

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using OpenAI API.

        Args:
            texts: List of texts to generate embeddings for

        Returns:
            List of embedding vectors, one per input text
        """
        try:
            return await generate_openai_embeddings(
                texts,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.embedding_model,
            )
        except Exception as e:
            memory_logger.error(f"OpenAI embedding generation failed: {e}")
            raise

    async def test_connection(self) -> bool:
        """Test OpenAI connection with timeout.

        Returns:
            True if connection successful, False otherwise
        """

        try:
            # Add 10-second timeout to prevent hanging on API calls
            async with asyncio.timeout(10):
                client = _get_openai_client(api_key=self.api_key, base_url=self.base_url, is_async=True)
                await client.models.list()
                return True
        except asyncio.TimeoutError:
            memory_logger.error(f"OpenAI connection test timed out after 10s - check network connectivity and API endpoint")
            return False
        except Exception as e:
            memory_logger.error(f"OpenAI connection test failed: {e}")
            return False

    async def propose_memory_actions(
        self,
        retrieved_old_memory: List[Dict[str, str]] | List[str],
        new_facts: List[str],
        custom_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Use OpenAI chat completion with enhanced prompt to propose memory actions.

        Args:
            retrieved_old_memory: List of existing memories for context
            new_facts: List of new facts to process
            custom_prompt: Optional custom prompt to override default

        Returns:
            Dictionary containing proposed memory actions
        """
        try:
            # Generate the complete prompt using the helper function
            memory_logger.debug(f"🧠 Facts passed to prompt builder: {new_facts}")
            update_memory_messages = build_update_memory_messages(
                retrieved_old_memory,
                new_facts,
                custom_prompt
            )
            memory_logger.debug(f"🧠 Generated prompt user content: {update_memory_messages[1]['content'][:200]}...")

            client = _get_openai_client(api_key=self.api_key, base_url=self.base_url, is_async=True)
            response = await client.chat.completions.create(
                model=self.model,
                messages=update_memory_messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            content = (response.choices[0].message.content or "").strip()
            if not content:
                return {}

            xml = extract_assistant_xml_from_openai_response(response)
            memory_logger.info(f"OpenAI propose_memory_actions xml: {xml}")
            items = parse_memory_xml(xml)
            memory_logger.info(f"OpenAI propose_memory_actions items: {items}")
            result = items_to_json(items)
            # example {'memory': [{'id': '0', 'event': 'UPDATE', 'text': 'My name is John', 'old_memory': None}}
            memory_logger.info(f"OpenAI propose_memory_actions result: {result}")
            return result

        except Exception as e:
            memory_logger.error(f"OpenAI propose_memory_actions failed: {e}")
            return {}


class OllamaProvider(LLMProviderBase):
    """Ollama LLM provider implementation.
    
    Provides memory extraction, embedding generation, and memory action
    proposals using Ollama's GPT and embedding models.
    
    
    Use the openai provider for ollama with different environment variables
    
    os.environ["OPENAI_API_KEY"] = "ollama"  
    os.environ["OPENAI_BASE_URL"] = "http://localhost:11434/v1"
    os.environ["QDRANT_BASE_URL"] = "localhost"
    os.environ["OPENAI_EMBEDDER_MODEL"] = "erwan2/DeepSeek-R1-Distill-Qwen-1.5B:latest"
    
    """
    pass

def _parse_memories_content(content: str) -> List[str]:
    """
    Parse LLM content to extract memory strings.

    Handles cases where the model returns:
    - A JSON object after </think> with keys like "facts" and "preferences"
    - A plain JSON array of strings
    - Non-JSON text (fallback to single memory)
    """
    try:
        # Try robust extraction first (handles </think> and mixed output)
        parsed = extract_json_from_text(content)
        if isinstance(parsed, dict):
            collected: List[str] = []
            for key in ("facts", "preferences"):
                value = parsed.get(key)
                if isinstance(value, list):
                    collected.extend(
                        [str(item).strip() for item in value if str(item).strip()]
                    )
            # If the dict didn't contain expected keys, try to flatten any list values
            if not collected:
                for value in parsed.values():
                    if isinstance(value, list):
                        collected.extend(
                            [str(item).strip() for item in value if str(item).strip()]
                        )
            if collected:
                return collected
    except Exception:
        # Continue to other strategies
        pass

    # If content includes </think>, try parsing the post-think segment directly
    if "</think>" in content:
        post_think = content.split("</think>", 1)[1].strip()
        if post_think:
            parsed_list = _try_parse_list_or_object(post_think)
            if parsed_list is not None:
                return parsed_list

    # Try to parse the whole content as a JSON list or object
    parsed_list = _try_parse_list_or_object(content)
    if parsed_list is not None:
        return parsed_list

    # Fallback: treat as a single memory string
    return [content] if content else []


def _try_parse_list_or_object(text: str) -> List[str] | None:
    """Try to parse text as JSON list or object and extract strings."""
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
        if isinstance(data, dict):
            collected: List[str] = []
            for key in ("facts", "preferences"):
                value = data.get(key)
                if isinstance(value, list):
                    collected.extend(
                        [str(item).strip() for item in value if str(item).strip()]
                    )
            if collected:
                return collected
            # As a last attempt, flatten any list values
            for value in data.values():
                if isinstance(value, list):
                    collected.extend(
                        [str(item).strip() for item in value if str(item).strip()]
                    )
            return collected if collected else None
    except Exception:
        return None
