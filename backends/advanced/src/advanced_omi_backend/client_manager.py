"""
Client manager service for centralizing active_clients access and client-user relationships.

This service provides a centralized way to manage active client connections,
their state, and client-user relationships, allowing API endpoints to access
this information without tight coupling to the main.py module.
"""

import logging
import uuid
from typing import TYPE_CHECKING, Dict, Optional

import redis.asyncio as redis

from advanced_omi_backend.client import ClientState
from advanced_omi_backend.redis_factory import create_async_redis

if TYPE_CHECKING:
    from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

# Global client-to-user mappings
# These will be initialized by main.py
_client_to_user_mapping: Dict[str, str] = {}  # Active clients only
_all_client_user_mappings: Dict[str, str] = {}  # All clients including disconnected

# Redis client for cross-container client→user mapping
_redis_client: Optional[redis.Redis] = None


class ClientManager:
    """
    Centralized manager for active client connections and client-user relationships.

    This service provides atomic operations for client lifecycle management
    and serves as the single source of truth for active client state.
    """

    def __init__(self):
        self._active_clients: Dict[str, "ClientState"] = {}
        self._initialized = True  # Self-initializing, no external dict needed
        logger.info("ClientManager initialized as single source of truth")

    def is_initialized(self) -> bool:
        """Check if the client manager has been initialized."""
        return self._initialized

    def get_client(self, client_id: str) -> Optional["ClientState"]:
        """
        Get a specific client by ID.

        Args:
            client_id: The client ID to lookup

        Returns:
            ClientState object if found, None if client not found
        """
        return self._active_clients.get(client_id)

    def has_client(self, client_id: str) -> bool:
        """
        Check if a client is currently active.

        Args:
            client_id: The client ID to check

        Returns:
            True if client is active, False otherwise
        """
        return client_id in self._active_clients

    def is_client_active(self, client_id: str) -> bool:
        """
        Check if a client is currently active (alias for has_client).

        Args:
            client_id: The client ID to check

        Returns:
            True if client is active, False otherwise
        """
        return self.has_client(client_id)

    def get_all_clients(self) -> Dict[str, "ClientState"]:
        """
        Get all active clients.

        Returns:
            Dictionary of client_id -> ClientState mappings
        """
        return self._active_clients.copy()

    def get_client_count(self) -> int:
        """
        Get the number of active clients.

        Returns:
            Number of active clients
        """
        return len(self._active_clients)

    def create_client(
        self, client_id: str, user_id: str, user_email: Optional[str] = None
    ) -> "ClientState":
        """
        Atomically create and register a new client.

        This method ensures that client creation and registration happen atomically,
        eliminating race conditions.

        Args:
            client_id: Unique client identifier
            user_id: User ID who owns this client
            user_email: Optional user email

        Returns:
            Created ClientState object

        Raises:
            ValueError: If client_id already exists
        """
        if client_id in self._active_clients:
            raise ValueError(f"Client {client_id} already exists")

        # Create client state
        client_state = ClientState(client_id, user_id, user_email)

        # Atomically add to internal storage and register mapping
        self._active_clients[client_id] = client_state
        register_client_user_mapping(client_id, user_id)

        logger.info(f"✅ Created and registered client {client_id} for user {user_id}")
        return client_state

    async def remove_client_with_cleanup(self, client_id: str) -> bool:
        """
        Atomically remove client with full cleanup.

        Args:
            client_id: Client identifier to remove

        Returns:
            True if client was removed, False if client didn't exist
        """
        if client_id not in self._active_clients:
            logger.warning(f"Attempted to remove non-existent client {client_id}")
            return False

        # Get client state for cleanup
        client_state = self._active_clients[client_id]

        # Call client's disconnect method for proper cleanup
        await client_state.disconnect()

        # Atomically remove from storage and deregister mapping
        del self._active_clients[client_id]
        unregister_client_user_mapping(client_id)

        logger.info(f"✅ Removed and cleaned up client {client_id}")
        return True

    def get_all_client_ids(self) -> list[str]:
        """
        Get list of all active client IDs.

        Returns:
            List of active client IDs
        """
        return list(self._active_clients.keys())

    def get_client_info_summary(self) -> list:
        """
        Get summary information about all active clients.

        Returns:
            List of client info dictionaries suitable for API responses
        """
        client_info = []
        for client_id, client_state in self._active_clients.items():
            client_data = {
                "client_id": client_id,
                "connected": client_state.connected,
                "user_email": client_state.user_email,
            }
            client_info.append(client_data)

        return client_info

    # Client-user relationship methods
    def client_belongs_to_user(self, client_id: str, user_id: str) -> bool:
        """
        Check if a client belongs to a specific user.

        Args:
            client_id: The client ID to check
            user_id: The user ID to check ownership against

        Returns:
            True if the client belongs to the user, False otherwise
        """
        # Check in all mappings (includes disconnected clients)
        mapped_user_id = _all_client_user_mappings.get(client_id)
        if mapped_user_id is None:
            logger.warning(f"Client {client_id} not found in user mapping")
            return False

        return mapped_user_id == user_id

    def get_user_clients_all(self, user_id: str) -> list[str]:
        """
        Get all client IDs (active and inactive) that belong to a specific user.

        Args:
            user_id: The user ID to get clients for

        Returns:
            List of client IDs belonging to the user
        """
        return [
            client_id
            for client_id, mapped_user_id in _all_client_user_mappings.items()
            if mapped_user_id == user_id
        ]

    def get_user_clients_active(self, user_id: str) -> list[str]:
        """
        Get active client IDs that belong to a specific user.

        Args:
            user_id: The user ID to get clients for

        Returns:
            List of active client IDs belonging to the user
        """
        return [
            client_id
            for client_id, mapped_user_id in _client_to_user_mapping.items()
            if mapped_user_id == user_id
        ]


# Global instance
_client_manager: Optional[ClientManager] = None


def get_client_manager() -> ClientManager:
    """
    Get the global client manager instance.

    Returns:
        ClientManager singleton instance
    """
    global _client_manager
    if _client_manager is None:
        _client_manager = ClientManager()
    return _client_manager


def register_client_user_mapping(client_id: str, user_id: str):
    """
    Register a client-user mapping for active clients.

    Args:
        client_id: The client ID
        user_id: The user ID that owns this client
    """
    _client_to_user_mapping[client_id] = user_id
    logger.info(f"✅ Registered active client {client_id} to user {user_id}")
    logger.info(f"📊 Active client mappings: {len(_client_to_user_mapping)} total")


def unregister_client_user_mapping(client_id: str):
    """
    Unregister a client-user mapping from active clients.

    Args:
        client_id: The client ID to unregister
    """
    if client_id in _client_to_user_mapping:
        user_id = _client_to_user_mapping.pop(client_id)
        logger.info(f"❌ Unregistered active client {client_id} from user {user_id}")
        logger.info(
            f"📊 Active client mappings: {len(_client_to_user_mapping)} remaining"
        )
    else:
        logger.warning(f"⚠️ Attempted to unregister non-existent client {client_id}")


async def track_client_user_relationship_async(
    client_id: str, user_id: str, ttl: int = 86400
):
    """
    Track that a client belongs to a user (async, writes to Redis for cross-container support).

    Args:
        client_id: The client ID
        user_id: The user ID that owns this client
        ttl: Time-to-live in seconds (default 24 hours)
    """
    _all_client_user_mappings[client_id] = user_id  # In-memory fallback

    if _redis_client:
        try:
            await _redis_client.setex(f"client:owner:{client_id}", ttl, user_id)
            logger.debug(
                f"✅ Tracked client {client_id} → user {user_id} in Redis (TTL: {ttl}s)"
            )
        except Exception as e:
            logger.warning(f"Failed to track client in Redis: {e}")
    else:
        logger.debug(
            f"Tracked client {client_id} relationship to user {user_id} (in-memory only)"
        )


def track_client_user_relationship(client_id: str, user_id: str):
    """
    Track that a client belongs to a user (sync version for backward compatibility).

    WARNING: This is synchronous and cannot use Redis. Use track_client_user_relationship_async()
    instead in async contexts for cross-container support.

    Args:
        client_id: The client ID
        user_id: The user ID that owns this client
    """
    _all_client_user_mappings[client_id] = user_id
    logger.debug(f"Tracked client {client_id} relationship to user {user_id}")


def client_belongs_to_user(client_id: str, user_id: str) -> bool:
    """
    Check if a client belongs to a specific user.

    Args:
        client_id: The client ID to check
        user_id: The user ID to check ownership against

    Returns:
        True if the client belongs to the user, False otherwise
    """
    # Check in all mappings (includes disconnected clients)
    mapped_user_id = _all_client_user_mappings.get(client_id)
    if mapped_user_id is None:
        logger.warning(f"Client {client_id} not found in user mapping")
        return False

    return mapped_user_id == user_id


def get_user_clients_all(user_id: str) -> list[str]:
    """
    Get all client IDs (active and inactive) that belong to a specific user.

    Args:
        user_id: The user ID to get clients for

    Returns:
        List of client IDs belonging to the user
    """
    return [
        client_id
        for client_id, mapped_user_id in _all_client_user_mappings.items()
        if mapped_user_id == user_id
    ]


def get_user_clients_active(user_id: str) -> list[str]:
    """
    Get active client IDs that belong to a specific user.

    Args:
        user_id: The user ID to get clients for

    Returns:
        List of active client IDs belonging to the user
    """
    logger.debug(f"🔍 Looking for active clients for user {user_id}")
    logger.debug(f"🔍 Available mappings: {_client_to_user_mapping}")

    user_clients = [
        client_id
        for client_id, mapped_user_id in _client_to_user_mapping.items()
        if mapped_user_id == user_id
    ]

    logger.debug(
        f"🔍 Found {len(user_clients)} active clients for user {user_id}: {user_clients}"
    )
    return user_clients


def initialize_redis_for_client_manager():
    """Initialize the shared Redis client for cross-container client→user mapping."""
    global _redis_client
    _redis_client = create_async_redis(decode_responses=True)
    logger.info("✅ ClientManager Redis initialized")


async def get_client_owner_async(client_id: str) -> Optional[str]:
    """
    Get the user ID that owns a specific client (async Redis lookup).

    Args:
        client_id: The client ID to look up

    Returns:
        User ID if found, None otherwise
    """
    if _redis_client:
        try:
            user_id = await _redis_client.get(f"client:owner:{client_id}")
            return user_id
        except Exception as e:
            logger.warning(f"Redis lookup failed for client {client_id}: {e}")

    # Fallback to in-memory mapping
    return _all_client_user_mappings.get(client_id)


def get_client_owner(client_id: str) -> Optional[str]:
    """
    Get the user ID that owns a specific client (sync version for backward compatibility).

    WARNING: This is synchronous and cannot use Redis. Use get_client_owner_async() instead
    in async contexts for cross-container support.

    Args:
        client_id: The client ID to look up

    Returns:
        User ID if found, None otherwise
    """
    return _all_client_user_mappings.get(client_id)


# FastAPI dependency function
async def get_client_manager_dependency() -> ClientManager:
    """
    FastAPI dependency to inject the client manager into route handlers.

    Usage:
        @router.get("/some-endpoint")
        async def some_endpoint(client_manager: ClientManager = Depends(get_client_manager_dependency)):
            clients = client_manager.get_all_clients()
            ...
    """
    client_manager = get_client_manager()
    if not client_manager.is_initialized():
        logger.error("ClientManager dependency requested but not initialized")
        # In a real application, you might want to raise an exception here
        # For now, we'll return the uninitialized manager and let the caller handle it
    return client_manager


def user_id_prefix(user: "User") -> str:
    """The user-scoping prefix of every client id: last 6 of the ObjectId.

    The single place this slice is taken. Constructing it inline is how the upload
    path grew a second, differently-normalizing ``generate_client_id`` that gave the
    same physical device a different identity than the WebSocket path did.
    """
    return str(user.id)[-6:]


def owns_client_id(user: "User", client_id: str) -> bool:
    """Whether ``client_id`` belongs to ``user`` by its prefix.

    Prefix ownership is a *weak* check — it is derived from a 6-character slice, so it
    is only safe where the caller has already established the user. Use the registry
    (``User.has_client`` / ``get_user_by_client_id``) when the answer must be
    authoritative.
    """
    return (client_id or "").startswith(user_id_prefix(user))


def synthetic_client_id(user: "User", purpose: str) -> str:
    """A client id for server-generated work that has no device behind it.

    Kept apart from ``generate_client_id`` rather than folded into it: that function
    sanitizes and truncates *user-supplied* device names to 10 characters, which would
    silently rewrite a server-controlled label (``annotation-import`` →
    ``annotation``) and split existing data across two ids. ``purpose`` is a literal in
    Chronicle's own source, so it needs neither sanitizing nor bounding.
    """
    return f"{user_id_prefix(user)}-{purpose}"


def generate_client_id(user: "User", device_name: Optional[str] = None) -> str:
    """
    Generate a STABLE client_id in the format: user_id_suffix-device_suffix

    The one constructor for a *device's* identity, on every ingress path. The upload
    controller used to have its own copy that interpolated the raw device name, so
    ``MyPhone!`` became ``abc123-MyPhone!`` on upload and ``abc123-myphone`` over the
    WebSocket — one physical device, two identities, two registry entries.

    The client_id is deterministic for a given (user, device_name): the same device
    reconnecting always maps to the same id. This is the device's stable identity —
    a reconnect reuses it (and evict-on-reconnect collapses any lingering connection
    onto the new one) instead of minting a fresh id every time.

    NOTE: there is deliberately NO uniqueness counter. The old `-N` counter, checked
    against the ever-growing `user.registered_clients` history, turned every reconnect
    of one device into a brand-new id (havpe, havpe-2, … havpe-286) and made the
    registry and the Network page balloon. Device names are expected to be unique per
    physical device (set per relay/app); two distinct devices sharing a name should be
    renamed, not silently disambiguated into divergent identities.

    Args:
        user: The User object
        device_name: Optional device name (e.g., 'havpe', 'phone', 'tablet')

    Returns:
        client_id as user_id_suffix-device_suffix (or user_id_suffix-<uuid> when no
        device name is supplied).
    """
    user_id_suffix = user_id_prefix(user)

    if device_name:
        # Sanitize device name: lowercase, alphanumeric + hyphens only, max 10 chars
        sanitized_device = "".join(
            c for c in device_name.lower() if c.isalnum() or c == "-"
        )[:10]
        return f"{user_id_suffix}-{sanitized_device}"

    # No device name → fall back to a UUID suffix (still unique, just not stable).
    uuid_suffix = str(uuid.uuid4())[:8]
    return f"{user_id_suffix}-{uuid_suffix}"
