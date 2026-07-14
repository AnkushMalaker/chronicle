import pytest
from pydantic import ValidationError

from advanced_omi_backend.models.vault_sync import PairRequest

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
