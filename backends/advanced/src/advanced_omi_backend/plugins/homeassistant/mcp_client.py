"""
MCP client for communicating with Home Assistant's MCP Server.

Home Assistant exposes an MCP server at /api/mcp that provides tools
for controlling smart home devices.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """MCP protocol error"""
    pass


class HAMCPClient:
    """
    MCP Client for Home Assistant's /api/mcp endpoint.

    Implements the Model Context Protocol for communicating with
    Home Assistant's built-in MCP server.
    """

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        """
        Initialize the MCP client.

        Args:
            base_url: Base URL of Home Assistant (e.g., http://localhost:8123)
            token: Long-lived access token for authentication
            timeout: Request timeout in seconds

        """
        self.base_url = base_url.rstrip('/')
        self.mcp_url = f"{self.base_url}/api/mcp"
        self.token = token
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=timeout)
        self._request_id = 0

    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

    def _next_request_id(self) -> int:
        """Generate next request ID"""
        self._request_id += 1
        return self._request_id

    async def _send_mcp_request(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Send MCP protocol request to Home Assistant.

        Args:
            method: MCP method name (e.g., "tools/list", "tools/call")
            params: Optional method parameters

        Returns:
            Response data from MCP server

        Raises:
            MCPError: If request fails or returns an error
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": method
        }

        if params:
            payload["params"] = params

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            logger.debug(f"MCP Request: {method} with params: {params}")
            response = await self.client.post(
                self.mcp_url,
                json=payload,
                headers=headers
            )
            response.raise_for_status()

            data = response.json()

            # Check for JSON-RPC error
            if "error" in data:
                error = data["error"]
                raise MCPError(f"MCP Error {error.get('code')}: {error.get('message')}")

            return data.get("result", {})

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling MCP endpoint: {e.response.status_code}")
            raise MCPError(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error calling MCP endpoint: {e}")
            raise MCPError(f"Request failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error calling MCP endpoint: {e}")
            raise MCPError(f"Unexpected error: {e}")

    async def list_tools(self) -> List[Dict[str, Any]]:
        """
        Get list of available MCP tools from Home Assistant.

        Returns:
            List of tool definitions with schema

        Example tool:
            {
                "name": "turn_on",
                "description": "Turn on a light or switch",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "entity_id": {"type": "string"}
                    }
                }
            }
        """
        result = await self._send_mcp_request("tools/list")
        tools = result.get("tools", [])
        logger.info(f"Retrieved {len(tools)} tools from Home Assistant MCP")
        return tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool via MCP.

        Args:
            tool_name: Name of the tool to call (e.g., "turn_on", "turn_off")
            arguments: Tool arguments (e.g., {"entity_id": "light.hall_light"})

        Returns:
            Tool execution result

        Raises:
            MCPError: If tool execution fails

        Example:
            >>> await client.call_tool("turn_off", {"entity_id": "light.hall_light"})
            {"success": True}
        """
        params = {
            "name": tool_name,
            "arguments": arguments
        }

        logger.info(f"Calling MCP tool '{tool_name}' with args: {arguments}")
        result = await self._send_mcp_request("tools/call", params)

        # MCP tool results are wrapped in content blocks
        content = result.get("content", [])
        if content and isinstance(content, list):
            # Extract text content from first block
            first_block = content[0]
            if isinstance(first_block, dict) and first_block.get("type") == "text":
                return {"result": first_block.get("text"), "success": True}

        return result

    async def test_connection(self) -> bool:
        """
        Test connection to Home Assistant MCP server.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            tools = await self.list_tools()
            logger.info(f"MCP connection test successful ({len(tools)} tools available)")
            return True
        except Exception as e:
            logger.error(f"MCP connection test failed: {e}")
            return False

    async def _render_template(self, template: str) -> Any:
        """
        Render a Home Assistant template using the Template API.

        Args:
            template: Jinja2 template string (e.g., "{{ areas() }}")

        Returns:
            Rendered template result (parsed as JSON if possible)

        Raises:
            MCPError: If template rendering fails

        Example:
            >>> await client._render_template("{{ areas() }}")
            ["study", "living_room", "bedroom"]
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        payload = {"template": template}

        try:
            logger.debug(f"Rendering template: {template}")
            response = await self.client.post(
                f"{self.base_url}/api/template",
                json=payload,
                headers=headers
            )
            response.raise_for_status()

            result = response.text.strip()

            # Try to parse as JSON (for lists, dicts)
            if result.startswith('[') or result.startswith('{'):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse template result as JSON: {result}")
                    return result

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error rendering template: {e.response.status_code}")
            raise MCPError(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error rendering template: {e}")
            raise MCPError(f"Request failed: {e}")

    async def fetch_areas(self) -> List[str]:
        """
        Fetch all areas from Home Assistant using Template API.

        Returns:
            List of area names

        Example:
            >>> await client.fetch_areas()
            ["study", "living_room", "bedroom"]
        """
        template = "{{ areas() | to_json }}"
        areas = await self._render_template(template)

        if isinstance(areas, list):
            logger.info(f"Fetched {len(areas)} areas from Home Assistant")
            return areas
        else:
            logger.warning(f"Unexpected areas format: {type(areas)}")
            return []

    async def fetch_area_entities(self, area_name: str) -> List[str]:
        """
        Fetch all entity IDs in a specific area.

        Args:
            area_name: Name of the area

        Returns:
            List of entity IDs in the area

        Example:
            >>> await client.fetch_area_entities("study")
            ["light.tubelight_3", "switch.desk_fan"]
        """
        template = f"{{{{ area_entities('{area_name}') | to_json }}}}"
        entities = await self._render_template(template)

        if isinstance(entities, list):
            logger.info(f"Fetched {len(entities)} entities from area '{area_name}'")
            return entities
        else:
            logger.warning(f"Unexpected entities format for area '{area_name}': {type(entities)}")
            return []

    async def fetch_entity_states(self) -> Dict[str, Dict]:
        """
        Fetch all entity states from Home Assistant.

        Returns:
            Dict mapping entity_id to state data (includes attributes, area_id)

        Example:
            >>> await client.fetch_entity_states()
            {
                "light.tubelight_3": {
                    "state": "on",
                    "attributes": {"friendly_name": "Study Light", ...},
                    "area_id": "study"
                }
            }
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            logger.debug("Fetching all entity states")
            response = await self.client.get(
                f"{self.base_url}/api/states",
                headers=headers
            )
            response.raise_for_status()

            states = response.json()
            entity_details = {}

            # Enrich with area information
            for state in states:
                entity_id = state.get('entity_id')
                if entity_id:
                    # Get area_id using Template API
                    try:
                        area_template = f"{{{{ area_id('{entity_id}') }}}}"
                        area_id = await self._render_template(area_template)
                        state['area_id'] = area_id if area_id else None
                    except Exception as e:
                        logger.debug(f"Failed to get area for {entity_id}: {e}")
                        state['area_id'] = None

                    entity_details[entity_id] = state

            logger.info(f"Fetched {len(entity_details)} entity states")
            return entity_details

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching states: {e.response.status_code}")
            raise MCPError(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error fetching states: {e}")
            raise MCPError(f"Request failed: {e}")

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_ids: List[str],
        **parameters
    ) -> Dict[str, Any]:
        """
        Call a Home Assistant service directly via REST API.

        Args:
            domain: Service domain (e.g., "light", "switch")
            service: Service name (e.g., "turn_on", "turn_off")
            entity_ids: List of entity IDs to target
            **parameters: Additional service parameters (e.g., brightness_pct=50)

        Returns:
            Service call response

        Example:
            >>> await client.call_service("light", "turn_on", ["light.study"], brightness_pct=50)
            [{"entity_id": "light.study", "state": "on"}]
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        payload = {
            "entity_id": entity_ids,
            **parameters
        }

        service_url = f"{self.base_url}/api/services/{domain}/{service}"

        try:
            logger.info(f"Calling service {domain}.{service} for {len(entity_ids)} entities")
            logger.debug(f"Service payload: {payload}")

            response = await self.client.post(
                service_url,
                json=payload,
                headers=headers
            )
            response.raise_for_status()

            result = response.json()
            logger.info(f"Service call successful: {domain}.{service}")
            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling service: {e.response.status_code}")
            raise MCPError(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error calling service: {e}")
            raise MCPError(f"Request failed: {e}")

    async def discover_entities(self) -> Dict[str, Dict]:
        """
        Discover available entities from MCP tools.

        Parses the available tools to build an index of entities
        that can be controlled.

        Returns:
            Dict mapping entity_id to metadata
        """
        tools = await self.list_tools()
        entities = {}

        for tool in tools:
            # Extract entity information from tool schemas
            # This will depend on how HA MCP structures its tools
            # For now, we'll just log what we find
            logger.debug(f"Tool: {tool.get('name')} - {tool.get('description')}")

        # TODO: Parse tool schemas to extract entity_id information
        # For now, return empty dict - will be populated based on actual HA MCP response

        return entities
