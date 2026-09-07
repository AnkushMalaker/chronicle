import pytest
from pydantic import ValidationError

from backend.models.vault_sync import PairRequest
from backend.services.memory.scope import MemoryScope, MemoryScopeResolver
from backend.services.vault_sync import VaultSyncBroker

VALID_DEVICE_ID = "S7UKX27-GI7ZTXS-GC6RKUA-7AJGZ44-C6NAYEB-HSKTJQK-KJHU2NO-CWV7EQW"


def test_pair_request_accepts_canonical_syncthing_device_id():
    request = PairRequest(device_id=VALID_DEVICE_ID)

    assert request.device_id == VALID_DEVICE_ID


@pytest.mark.parametrize(
    "device_id",
    [
        "../../rest/system/shutdown",
        f"{VALID_DEVICE_ID}?ignored=true",
        VALID_DEVICE_ID.replace("-", ""),
        "not-a-device-id",
    ],
)
def test_pair_request_rejects_noncanonical_device_id(device_id):
    with pytest.raises(ValidationError, match="device_id"):
        PairRequest(device_id=device_id)


def test_scoped_folder_uses_uuid_identity_and_is_not_nested_in_main(tmp_path):
    resolver = MemoryScopeResolver(tmp_path)
    broker = VaultSyncBroker(resolver)
    space_id = "9f3523c8-af75-469d-995a-7179531f3fc8"

    folder = broker.folder(
        MemoryScope("user-1", space_id), space_name="Same name as another space"
    )

    assert folder.folder_id == f"space-user-1-{space_id}"
    assert folder.backend_path == (
        tmp_path / "memory_spaces" / "user-1" / space_id / "vault"
    )
    assert resolver.main_root("user-1") not in folder.backend_path.parents
    assert folder.syncthing_path == f"/memory-spaces/user-1/{space_id}/vault"
