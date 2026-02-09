"""Button event parsing for Omi BLE button characteristic."""

import struct
from enum import IntEnum


class ButtonState(IntEnum):
    IDLE = 0
    SINGLE_TAP = 1
    DOUBLE_TAP = 2
    LONG_PRESS = 3
    PRESS = 4
    RELEASE = 5


def parse_button_event(data: bytes) -> ButtonState:
    """Parse the button event payload into a ButtonState.

    Payload is two little-endian uint32 values: [state, 0].
    """
    if len(data) < 8:
        raise ValueError(f"Expected 8 bytes for button event, got {len(data)}")
    state, _unused = struct.unpack("<II", data[:8])
    return ButtonState(state)
