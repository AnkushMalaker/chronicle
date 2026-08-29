from advanced_omi_backend.client_manager import ClientManager


async def test_old_connection_cleanup_cannot_remove_replacement_client():
    manager = ClientManager()
    old = manager.create_client("client-1", "user-1")
    old.socket_id = "connection-old"

    # Model the atomic reconnect replacement performed by the WebSocket setup path.
    replacement = type(old)("client-1", "user-1")
    replacement.socket_id = "connection-new"
    manager._active_clients["client-1"] = replacement

    removed = await manager.remove_client_lease("client-1", "connection-old")

    assert removed is False
    assert manager.get_client("client-1") is replacement


async def test_current_connection_cleanup_removes_exact_lease():
    manager = ClientManager()
    state = manager.create_client("client-1", "user-1")
    state.socket_id = "connection-current"

    removed = await manager.remove_client_lease("client-1", "connection-current")

    assert removed is True
    assert manager.get_client("client-1") is None
