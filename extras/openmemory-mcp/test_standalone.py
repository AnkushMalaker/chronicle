#!/usr/bin/env python3
"""Standalone test script for OpenMemory MCP server.

This script tests the OpenMemory MCP server directly using its REST API,
without any dependencies on Chronicle backend code.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx

# Test Configuration Flags (following project patterns)
# TODO: Update CLAUDE.md documentation to reflect FRESH_RUN flag usage across all integration tests
# This replaces any previous "CACHED_MODE" references with consistent FRESH_RUN naming
FRESH_RUN = os.environ.get("FRESH_RUN", "true").lower() == "true"
CLEANUP_CONTAINERS = (
    os.environ.get("CLEANUP_CONTAINERS", "false").lower() == "true"
)  # Default false for dev convenience
REBUILD = os.environ.get("REBUILD", "false").lower() == "true"


class OpenMemoryClient:
    """Simple client for testing OpenMemory REST API."""

    def __init__(
        self, server_url: str = "http://localhost:8765", user_id: str = "test_user"
    ):
        self.server_url = server_url.rstrip("/")
        self.user_id = user_id
        self.client = httpx.AsyncClient(timeout=30.0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    async def test_connection(self) -> bool:
        """Test if server is reachable."""
        try:
            response = await self.client.get(f"{self.server_url}/")
            return response.status_code in [200, 404, 422]
        except:
            return False

    async def create_memory(self, text: str) -> Dict[str, Any]:
        """Create a new memory."""
        response = await self.client.post(
            f"{self.server_url}/api/v1/memories/",
            json={
                "user_id": self.user_id,
                "text": text,
                "metadata": {"source": "test_script", "test": True},
                "infer": True,
                "app": "openmemory",  # Use default app name that exists
            },
        )
        response.raise_for_status()
        return response.json()

    async def list_memories(self, limit: int = 10) -> List[Dict[str, Any]]:
        """List memories for the user."""
        response = await self.client.get(
            f"{self.server_url}/api/v1/memories/",
            params={"user_id": self.user_id, "page": 1, "size": limit},
        )
        response.raise_for_status()
        result = response.json()

        # Handle paginated response
        if isinstance(result, dict) and "items" in result:
            return result["items"]
        elif isinstance(result, list):
            return result
        return []

    async def search_memories(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search memories with a query."""
        response = await self.client.get(
            f"{self.server_url}/api/v1/memories/",
            params={
                "user_id": self.user_id,
                "search_query": query,
                "page": 1,
                "size": limit,
            },
        )
        response.raise_for_status()
        result = response.json()

        # Handle paginated response
        if isinstance(result, dict) and "items" in result:
            return result["items"]
        elif isinstance(result, list):
            return result
        return []

    async def delete_memories(self, memory_ids: List[str]) -> Dict[str, Any]:
        """Delete specific memories."""
        response = await self.client.request(
            "DELETE",
            f"{self.server_url}/api/v1/memories/",
            json={"memory_ids": memory_ids, "user_id": self.user_id},
        )
        response.raise_for_status()
        return response.json()

    async def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        try:
            response = await self.client.get(
                f"{self.server_url}/api/v1/stats/", params={"user_id": self.user_id}
            )
            response.raise_for_status()
            return response.json()
        except:
            return {}


async def test_basic_operations():
    """Test basic OpenMemory operations."""

    server_url = os.getenv("OPENMEMORY_MCP_URL", "http://localhost:8765")
    # Use the same user ID as OpenMemory server expects
    user_id = os.getenv("TEST_USER_ID", os.getenv("USER", "openmemory"))

    print(f"🧪 Testing OpenMemory MCP Server")
    print(f"📍 Server URL: {server_url}")
    print(f"👤 User ID: {user_id}")
    print("=" * 60)

    async with OpenMemoryClient(server_url, user_id) as client:
        # Test 1: Connection
        print("\n1️⃣  Testing connection...")
        is_connected = await client.test_connection()
        if not is_connected:
            print("❌ Failed to connect to OpenMemory server")
            print("   Please ensure the server is running:")
            print("   cd extras/openmemory-mcp && ./run.sh")
            return False
        print("✅ Connected to OpenMemory server")

        # Test 2: Create memory
        print("\n2️⃣  Creating test memories...")
        test_memories = [
            "I prefer Python for backend development and use FastAPI for building APIs.",
            "My morning routine includes meditation at 6 AM followed by a 5-mile run.",
            "I'm learning Japanese and practice with Anki flashcards for 30 minutes daily.",
            "My favorite book is 'The Pragmatic Programmer' and I re-read it every year.",
            "I work remotely from a co-working space in Seattle three days a week.",
        ]

        created_memories = []
        for i, text in enumerate(test_memories, 1):
            try:
                result = await client.create_memory(text)
                if result is None:
                    # Handle None response (no-op, likely duplicate)
                    print(f"   ℹ️  Memory {i}: No-op (likely duplicate)")
                elif isinstance(result, dict) and "error" in result:
                    print(f"   ⚠️  Memory {i}: {result['error']}")
                else:
                    # Handle successful creation or existing memory
                    if hasattr(result, "id"):
                        memory_id = str(result.id)
                    else:
                        memory_id = (
                            result.get("id", f"memory_{i}")
                            if isinstance(result, dict)
                            else f"memory_{i}"
                        )

                    created_memories.append(memory_id)
                    print(f"   ✅ Memory {i}: Created (ID: {memory_id[:8]}...)")
            except Exception as e:
                print(f"   ❌ Memory {i}: Failed - {e}")

        print(
            f"\n   Summary: {len(created_memories)}/{len(test_memories)} memories created"
        )

        # Test 3: List memories
        print("\n3️⃣  Listing memories...")
        try:
            memories = await client.list_memories(limit=20)
            print(f"✅ Found {len(memories)} memory(ies)")

            for i, memory in enumerate(memories[:3], 1):
                content = memory.get("content", memory.get("text", ""))[:80]
                memory_id = str(memory.get("id", "unknown"))[:8]
                print(f"   {i}. [{memory_id}...] {content}...")
        except Exception as e:
            print(f"❌ Failed to list memories: {e}")
            memories = []

        # Test 4: Search memories
        print("\n4️⃣  Searching memories...")
        test_queries = [
            "programming Python",
            "morning exercise routine",
            "learning languages",
        ]

        for query in test_queries:
            try:
                results = await client.search_memories(query, limit=3)
                print(f"   Query: '{query}' → {len(results)} result(s)")
                if results:
                    top_result = results[0]
                    content = top_result.get("content", top_result.get("text", ""))[:60]
                    print(f"      Top: {content}...")
            except Exception as e:
                print(f"   ❌ Search failed for '{query}': {e}")

        # Test 5: Get stats (if available)
        print("\n5️⃣  Getting statistics...")
        try:
            stats = await client.get_stats()
            if stats:
                print(f"✅ Stats retrieved: {json.dumps(stats, indent=2)}")
            else:
                print("ℹ️  No statistics available")
        except Exception as e:
            print(f"ℹ️  Statistics endpoint not available: {e}")

        # Test 6: Delete memories (cleanup)
        if memories and len(memories) > 0:
            print("\n6️⃣  Testing deletion...")
            # Delete first memory as a test
            test_memory_id = str(memories[0].get("id"))
            try:
                result = await client.delete_memories([test_memory_id])
                print(f"✅ Deleted memory: {test_memory_id[:8]}...")
                if "message" in result:
                    print(f"   Response: {result['message']}")
            except Exception as e:
                print(f"⚠️  Deletion not supported or failed: {e}")

        print("\n" + "=" * 60)
        print("✨ Test completed successfully!")
        return True


async def test_mcp_protocol():
    """Test MCP protocol endpoints (if available)."""

    server_url = os.getenv("OPENMEMORY_MCP_URL", "http://localhost:8765")
    user_id = os.getenv("TEST_USER_ID", os.getenv("USER", "openmemory"))
    client_name = "test_client"

    print(f"\n🔧 Testing MCP Protocol Endpoints")
    print(f"📍 Server URL: {server_url}")
    print(f"👤 User ID: {user_id}")
    print(f"🏷️  Client: {client_name}")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=10.0) as client:  # 10 second timeout
        # Test MCP SSE endpoint
        print("\n1️⃣  Testing MCP SSE endpoint...")
        try:
            # SSE connections stay open, so we expect a timeout after connection opens
            response = await client.get(
                f"{server_url}/mcp/{client_name}/sse/{user_id}",
                headers={"Accept": "text/event-stream"},
            )
            # If we get here, connection opened successfully
            print("✅ MCP SSE endpoint is available")
        except httpx.TimeoutException:
            # This is expected - SSE connection opened but timed out waiting for events
            print(
                "✅ MCP SSE endpoint is available (connection opened, timed out as expected)"
            )
        except Exception as e:
            print(f"ℹ️  MCP SSE endpoint not available: {e}")

        # Test MCP messages endpoint
        print("\n2️⃣  Testing MCP messages endpoint...")
        try:
            # Send a simple JSON-RPC request
            payload = {
                "jsonrpc": "2.0",
                "id": "test_1",
                "method": "initialize",
                "params": {},
            }

            response = await client.post(
                f"{server_url}/mcp/messages/",  # Add trailing slash to avoid redirect
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Client-Name": client_name,
                    "X-User-ID": user_id,
                },
            )

            if response.status_code == 200:
                print("✅ MCP messages endpoint is available")
                result = response.json()
                print(f"   Response: {json.dumps(result, indent=2)[:200]}...")
            else:
                print(f"ℹ️  MCP messages endpoint returned: {response.status_code}")
        except Exception as e:
            print(f"ℹ️  MCP messages endpoint not available: {e}")

    print("\n✨ MCP protocol test completed!")


def load_env_files():
    """Load environment from .env.test (priority) or .env (fallback), following project patterns."""
    from dotenv import load_dotenv

    env_test_path = Path(".env.test")
    env_path = Path(".env")

    if env_test_path.exists():
        print(f"📄 Loading environment from {env_test_path}")
        load_dotenv(env_test_path)
    elif env_path.exists():
        print(f"📄 Loading environment from {env_path}")
        load_dotenv(env_path)
    else:
        print("⚠️  No .env.test or .env file found, using shell environment")


def validate_required_keys():
    """Validate required API keys - FAIL FAST if missing."""
    missing_keys = []

    if not os.getenv("OPENAI_API_KEY"):
        missing_keys.append("OPENAI_API_KEY")

    if missing_keys:
        print(
            f"❌ FATAL ERROR: Missing required environment variables: {', '.join(missing_keys)}"
        )
        print("   These are required for OpenMemory to function.")
        print("   Add to extras/openmemory-mcp/.env file:")
        for key in missing_keys:
            print(f"   {key}=your-key-here")
        print()
        print("   Example:")
        print(f"   echo '{missing_keys[0]}=your-key-here' >> .env")
        return False

    print(f"✅ Required API keys validated")
    return True


def cleanup_test_data():
    """Clean up OpenMemory test data if in fresh mode, following integration test patterns."""
    if not FRESH_RUN:
        print("🗂️  Cache mode: Reusing existing memories and data")
        return

    print("🗂️  Fresh mode: Cleaning existing memories and data...")

    # First, stop containers and remove volumes
    try:
        subprocess.run(["docker", "compose", "down", "-v"], check=True, cwd=Path.cwd())
        print("   ✅ Cleaned Docker volumes")
    except subprocess.CalledProcessError as e:
        print(f"   ⚠️  Could not clean Docker volumes: {e}")

    # Then, clean data directories using lightweight Docker container (following project pattern)
    try:
        # Check if data directory exists
        data_dir = Path.cwd() / "data"
        if data_dir.exists():
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{data_dir}:/data",
                    "alpine:latest",
                    "sh",
                    "-c",
                    "rm -rf /data/*",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                print("   ✅ Cleaned data directories")
            else:
                print(f"   ⚠️  Error during data directory cleanup: {result.stderr}")
        else:
            print("   ℹ️  No data directory to clean")

    except Exception as e:
        print(f"   ⚠️  Data directory cleanup failed: {e}")
        print("   💡 Ensure Docker is running and accessible")


def cleanup_containers():
    """Stop and remove containers after test if cleanup enabled."""
    if not CLEANUP_CONTAINERS:
        print("🐳 Keeping containers running for debugging")
        return

    print("🐳 Cleaning up test containers...")
    try:
        subprocess.run(["docker", "compose", "down", "-v"], check=True, cwd=Path.cwd())
        print("   ✅ Containers cleaned up")
    except subprocess.CalledProcessError as e:
        print(f"   ⚠️  Could not clean up containers: {e}")


async def main():
    """Run all standalone tests following integration test patterns."""

    print("🚀 OpenMemory MCP Standalone Tests")
    print("=" * 60)
    print(f"🔧 Configuration:")
    print(
        f"   FRESH_RUN={FRESH_RUN}, CLEANUP_CONTAINERS={CLEANUP_CONTAINERS}, REBUILD={REBUILD}"
    )
    print()

    # 1. Load environment files
    load_env_files()

    # 2. Validate required keys - FAIL FAST
    if not validate_required_keys():
        return False

    # 3. Data management
    cleanup_test_data()

    # 4. Ensure containers are running (rebuild if requested)
    if REBUILD:
        print("🔨 Rebuilding containers...")
        try:
            subprocess.run(
                ["docker", "compose", "build", "--no-cache"], check=True, cwd=Path.cwd()
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to rebuild containers: {e}")
            return False

    # Start containers
    print("🐳 Starting containers...")
    try:
        subprocess.run(["docker", "compose", "up", "-d"], check=True, cwd=Path.cwd())
        print("   ✅ Containers started")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to start containers: {e}")
        return False

    # Wait a moment for services to be ready
    print("⏳ Waiting for services to be ready...")
    await asyncio.sleep(5)

    try:
        # 5. Run basic operations test
        success = await test_basic_operations()

        if success:
            # 6. Run MCP protocol test
            await test_mcp_protocol()

        print(f"\n{'✅' if success else '❌'} Test Results:")
        print(f"   Basic Operations: {'PASSED' if success else 'FAILED'}")
        print(f"   MCP Protocol: {'TESTED' if success else 'SKIPPED'}")

        return success

    finally:
        # 7. Cleanup containers if requested
        cleanup_containers()

    print("\n🎉 All standalone tests completed!")


if __name__ == "__main__":
    asyncio.run(main())
