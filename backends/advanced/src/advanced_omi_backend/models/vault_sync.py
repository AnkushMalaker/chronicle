"""Request models for the vault sync pairing broker."""

from pydantic import BaseModel, Field

# Syncthing's canonical device ID is eight groups of seven base32 characters.
# Enforce that shape before the ID is interpolated into Syncthing REST paths.
_DEVICE_ID_PATTERN = r"^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$"


class PairRequest(BaseModel):
    device_id: str = Field(pattern=_DEVICE_ID_PATTERN)
    device_name: str = "mac"
