"""Client for communicating with OpenMemory servers.

This module provides a client interface for interacting with the official
OpenMemory servers using REST API endpoints for memory operations.
"""

import logging
import uuid
from typing import List, Dict, Any, Optional
import httpx

memory_logger = logging.getLogger("memory_service")


class MCPClient:
    """Client for communicating with OpenMemory servers.
    
    Uses the official OpenMemory REST API:
    - POST /api/v1/memories - Create new memory
    - GET /api/v1/memories - List memories
    - DELETE /api/v1/memories - Delete memories
    
    Attributes:
        server_url: Base URL of the OpenMemory server (default: http://localhost:8765)
        client_name: Client identifier for memory tagging
        user_id: User identifier for memory isolation
        timeout: Request timeout in seconds
        client: HTTP client instance
    """
    
    def __init__(self, server_url: str, client_name: str = "chronicle", user_id: str = "default", user_email: str = "", timeout: int = 30):
        """Initialize client for OpenMemory.

        Args:
            server_url: Base URL of the OpenMemory server
            client_name: Client identifier (used as app name)
            user_id: User identifier for memory isolation
            user_email: User email address for user metadata
            timeout: HTTP request timeout in seconds
        """
        self.server_url = server_url.rstrip('/')
        self.client_name = client_name
        self.user_id = user_id
        self.user_email = user_email
        self.timeout = timeout
        
        # Use custom CA certificate if available
        import os
        ca_bundle = os.getenv('REQUESTS_CA_BUNDLE')
        verify = ca_bundle if ca_bundle and os.path.exists(ca_bundle) else True
        
        self.client = httpx.AsyncClient(timeout=timeout, verify=verify)
        
    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def add_memories(self, text: str, metadata: Dict[str, Any] = None) -> List[str]:
        """Add memories to the OpenMemory server.
        
        Uses the REST API to create memories. OpenMemory will handle:
        - Memory extraction from text
        - Deduplication
        - Vector embedding and storage
        
        Args:
            text: Memory text to store
            
        Returns:
            List of created memory IDs
            
        Raises:
            MCPError: If the server request fails
        """
        try:
            # Get app_id first to handle duplicate app names
            apps_response = await self.client.get(f"{self.server_url}/api/v1/apps/")
            apps_response.raise_for_status()
            apps_data = apps_response.json()

            memory_logger.debug(f"Apps API response: {apps_data}")
            memory_logger.debug(f"Apps data type: {type(apps_data)}")
            if isinstance(apps_data, dict):
                memory_logger.debug(f"Apps dict keys: {apps_data.keys()}")
                if "apps" in apps_data:
                    memory_logger.debug(f"Number of apps: {len(apps_data['apps'])}")
                    memory_logger.debug(f"Apps list: {apps_data['apps']}")

            app_id = None
            if apps_data.get("apps"):
                # Find matching app by name, prefer one with most memories
                matching = [a for a in apps_data["apps"] if a["name"] == self.client_name]
                memory_logger.debug(f"Matching apps for '{self.client_name}': {matching}")
                if matching:
                    matching.sort(key=lambda x: x.get("total_memories_created", 0), reverse=True)
                    app_id = matching[0]["id"]
                    memory_logger.info(f"Found matching app with ID: {app_id}")
                else:
                    app_id = apps_data["apps"][0]["id"]
                    memory_logger.info(f"No matching app name, using first app ID: {app_id}")

            if not app_id:
                memory_logger.error("No apps found in OpenMemory - cannot create memory")
                raise MCPError("No apps found in OpenMemory")

            # Merge custom metadata with default metadata
            default_metadata = {
                "source": "chronicle",
                "client": self.client_name,
                "user_email": self.user_email
            }
            if metadata:
                default_metadata.update(metadata)

            # Use REST API endpoint for creating memories
            # The 'app' field can be either app name (string) or app UUID
            payload = {
                "user_id": self.user_id,
                "text": text,
                "app": self.client_name,
                "metadata": default_metadata,
                "infer": True
            }

            memory_logger.info(f"POSTing memory to {self.server_url}/api/v1/memories/ with payload={payload}")

            response = await self.client.post(
                f"{self.server_url}/api/v1/memories/",
                json=payload
            )

            response_body = response.text[:500] if response.status_code != 200 else "..."
            memory_logger.info(f"OpenMemory response: status={response.status_code}, body={response_body}, headers={dict(response.headers)}")

            response.raise_for_status()
            
            result = response.json()
            
            # Handle None result - OpenMemory returns None when no memory is created
            # (due to deduplication, insufficient content, etc.)
            if result is None:
                memory_logger.info("OpenMemory returned None - no memory created (likely deduplication)")
                return []
            
            # Handle error response
            if isinstance(result, dict) and "error" in result:
                memory_logger.error(f"OpenMemory error: {result['error']}")
                return []
            
            # Extract memory ID from response
            if isinstance(result, dict):
                memory_id = result.get("id") or str(uuid.uuid4())
                return [memory_id]
            elif isinstance(result, list):
                return [str(item.get("id", uuid.uuid4())) for item in result]
            
            # Default success response
            return [str(uuid.uuid4())]
            
        except httpx.HTTPError as e:
            memory_logger.error(f"HTTP error adding memories: {e}")
            raise MCPError(f"HTTP error: {e}")
        except Exception as e:
            memory_logger.error(f"Error adding memories: {e}")
            raise MCPError(f"Failed to add memories: {e}")
    
    async def search_memory(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search for memories using semantic similarity.
        
        Args:
            query: Search query text
            limit: Maximum number of results to return
            
        Returns:
            List of memory dictionaries with content and metadata
        """
        try:
            # First get the app_id for the default app
            apps_response = await self.client.get(f"{self.server_url}/api/v1/apps/")
            apps_response.raise_for_status()
            apps_data = apps_response.json()
            
            if not apps_data.get("apps") or len(apps_data["apps"]) == 0:
                memory_logger.warning("No apps found in OpenMemory MCP for search")
                return []
            
            # Find the app matching our client name, or use first app as fallback
            app_id = None
            for app in apps_data["apps"]:
                if app["name"] == self.client_name:
                    app_id = app["id"]
                    break
            
            if not app_id:
                memory_logger.warning(f"App '{self.client_name}' not found, using first available app")
                app_id = apps_data["apps"][0]["id"]
            
            # Use app-specific memories endpoint with search
            params = {
                "user_id": self.user_id,
                "search_query": query,
                "page": 1,
                "size": limit
            }
            
            response = await self.client.get(
                f"{self.server_url}/api/v1/apps/{app_id}/memories",
                params=params
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Extract memories from app-specific response format
            if isinstance(result, dict) and "memories" in result:
                memories = result["memories"]
            elif isinstance(result, list):
                memories = result
            else:
                memories = []
            
            # Format memories for Chronicle
            formatted_memories = []
            for memory in memories:
                formatted_memories.append({
                    "id": memory.get("id", str(uuid.uuid4())),
                    "content": memory.get("content", "") or memory.get("text", ""),
                    "metadata": memory.get("metadata_", {}) or memory.get("metadata", {}),
                    "created_at": memory.get("created_at"),
                    "score": memory.get("score", 0.0)  # No score from list API
                })
            
            return formatted_memories[:limit]
            
        except Exception as e:
            memory_logger.error(f"Error searching memories: {e}")
            return []
    
    async def list_memories(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all memories for the current user.
        
        Args:
            limit: Maximum number of memories to return
            
        Returns:
            List of memory dictionaries
        """
        try:
            # First get the app_id for the default app
            apps_response = await self.client.get(f"{self.server_url}/api/v1/apps/")
            apps_response.raise_for_status()
            apps_data = apps_response.json()
            
            if not apps_data.get("apps") or len(apps_data["apps"]) == 0:
                memory_logger.warning("No apps found in OpenMemory MCP")
                return []
            
            # Find the app matching our client name, or use first app as fallback
            app_id = None
            for app in apps_data["apps"]:
                if app["name"] == self.client_name:
                    app_id = app["id"]
                    break
            
            if not app_id:
                memory_logger.warning(f"App '{self.client_name}' not found, using first available app")
                app_id = apps_data["apps"][0]["id"]
            
            # Use app-specific memories endpoint
            params = {
                "user_id": self.user_id,
                "page": 1,
                "size": limit
            }
            
            response = await self.client.get(
                f"{self.server_url}/api/v1/apps/{app_id}/memories",
                params=params
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Extract memories from app-specific response format
            if isinstance(result, dict) and "memories" in result:
                memories = result["memories"]
            elif isinstance(result, list):
                memories = result
            else:
                memories = []
            
            # Format memories
            formatted_memories = []
            for memory in memories:
                formatted_memories.append({
                    "id": memory.get("id", str(uuid.uuid4())),
                    "content": memory.get("content", "") or memory.get("text", ""),
                    "metadata": memory.get("metadata_", {}) or memory.get("metadata", {}),
                    "created_at": memory.get("created_at")
                })
            
            return formatted_memories
            
        except Exception as e:
            memory_logger.error(f"Error listing memories: {e}")
            return []
    
    async def delete_all_memories(self) -> int:
        """Delete all memories for the current user.
        
        Note: OpenMemory may not support bulk delete via REST API.
        This is typically done through MCP tools for safety.
        
        Returns:
            Number of memories that were deleted
        """
        try:
            # First get all memory IDs
            memories = await self.list_memories(limit=1000)
            if not memories:
                return 0
            
            memory_ids = [m["id"] for m in memories]
            
            # Delete memories using the batch delete endpoint
            response = await self.client.request(
                "DELETE",
                f"{self.server_url}/api/v1/memories/",
                json={
                    "memory_ids": memory_ids,
                    "user_id": self.user_id
                }
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Extract count from response
            if isinstance(result, dict):
                if "message" in result:
                    # Parse message like "Successfully deleted 5 memories"
                    import re
                    match = re.search(r'(\d+)', result["message"])
                    return int(match.group(1)) if match else len(memory_ids)
                return result.get("deleted_count", len(memory_ids))
            
            return len(memory_ids)

        except Exception as e:
            memory_logger.error(f"Error deleting all memories: {e}")
            return 0

    async def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific memory by ID.

        Args:
            memory_id: ID of the memory to retrieve

        Returns:
            Memory dictionary if found, None otherwise
        """
        try:
            # Use the memories endpoint with specific ID
            response = await self.client.get(
                f"{self.server_url}/api/v1/memories/{memory_id}",
                params={"user_id": self.user_id}
            )

            if response.status_code == 404:
                memory_logger.warning(f"Memory not found: {memory_id}")
                return None

            response.raise_for_status()
            result = response.json()

            # Format memory for Chronicle
            if isinstance(result, dict):
                return {
                    "id": result.get("id", memory_id),
                    "content": result.get("content", "") or result.get("text", ""),
                    "metadata": result.get("metadata_", {}) or result.get("metadata", {}),
                    "created_at": result.get("created_at"),
                }

            return None

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            memory_logger.error(f"HTTP error getting memory: {e}")
            return None
        except Exception as e:
            memory_logger.error(f"Error getting memory: {e}")
            return None

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Update a specific memory's content and/or metadata.

        Args:
            memory_id: ID of the memory to update
            content: New content for the memory (if None, content is not updated)
            metadata: New metadata to merge with existing (if None, metadata is not updated)

        Returns:
            True if update succeeded, False otherwise
        """
        try:
            # Build update payload
            update_data: Dict[str, Any] = {"user_id": self.user_id}

            if content is not None:
                update_data["text"] = content

            if metadata is not None:
                update_data["metadata"] = metadata

            if len(update_data) == 1:  # Only user_id
                memory_logger.warning("No update data provided")
                return False

            # Use PUT to update memory
            response = await self.client.put(
                f"{self.server_url}/api/v1/memories/{memory_id}",
                json=update_data
            )

            response.raise_for_status()
            memory_logger.info(f"✅ Updated OpenMemory memory: {memory_id}")
            return True

        except httpx.HTTPStatusError as e:
            memory_logger.error(f"HTTP error updating memory: {e.response.status_code}")
            return False
        except Exception as e:
            memory_logger.error(f"Error updating memory: {e}")
            return False

    async def delete_memory(self, memory_id: str, user_id: Optional[str] = None, user_email: Optional[str] = None) -> bool:
        """Delete a specific memory by ID.
        
        Args:
            memory_id: ID of the memory to delete
            
        Returns:
            True if deletion succeeded, False otherwise
        """
        try:
            response = await self.client.request(
                "DELETE",
                f"{self.server_url}/api/v1/memories/",
                json={
                    "memory_ids": [memory_id],
                    "user_id": self.user_id
                }
            )
            response.raise_for_status()
            return True
            
        except Exception as e:
            memory_logger.warning(f"Error deleting memory {memory_id}: {e}")
            return False
    
    async def test_connection(self) -> bool:
        """Test connection to the OpenMemory server.
        
        Returns:
            True if server is reachable and responsive, False otherwise
        """
        try:
            # Test basic connectivity with health endpoint
            # OpenMemory may not have /health, try root or API endpoint
            for endpoint in ["/health", "/", "/api/v1/memories"]:
                try:
                    response = await self.client.get(
                        f"{self.server_url}{endpoint}",
                        params={"user_id": self.user_id, "page": 1, "size": 1}
                        if endpoint == "/api/v1/memories" else {}
                    )
                    if response.status_code in [200, 404, 422]:  # 404/422 means endpoint exists but params wrong
                        return True
                except:
                    continue
            
            return False
            
        except Exception as e:
            memory_logger.error(f"OpenMemory server connection test failed: {e}")
            return False


class MCPError(Exception):
    """Exception raised for MCP server communication errors."""
    pass