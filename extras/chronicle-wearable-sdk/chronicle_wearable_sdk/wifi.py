"""WiFi sync error codes matching firmware protocol."""

from enum import IntEnum


class WifiErrorCode(IntEnum):
    SUCCESS = 0x00
    INVALID_PACKET_LENGTH = 0x01
    INVALID_SETUP_LENGTH = 0x02
    SSID_LENGTH_INVALID = 0x03
    PASSWORD_LENGTH_INVALID = 0x04
    SESSION_ALREADY_RUNNING = 0x05
    HARDWARE_NOT_AVAILABLE = 0xFE
    UNKNOWN_COMMAND = 0xFF
