"""Platform-specific helpers to join/leave WiFi access points."""

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)


def join_wifi_ap(ssid: str, password: str, interface: str | None = None) -> bool:
    """Join a WiFi access point. Returns True on success."""
    system = platform.system()

    if system == "Darwin":
        iface = interface or "en0"
        result = subprocess.run(
            ["networksetup", "-setairportnetwork", iface, ssid, password],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Failed to join WiFi '%s': %s", ssid, result.stderr.strip())
        return result.returncode == 0

    elif system == "Linux":
        cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        if interface:
            cmd.extend(["ifname", interface])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("Failed to join WiFi '%s': %s", ssid, result.stderr.strip())
        return result.returncode == 0

    else:
        raise NotImplementedError(f"WiFi join not implemented for {system}")


def get_current_wifi(interface: str | None = None) -> str | None:
    """Get currently connected WiFi SSID (to restore later)."""
    system = platform.system()

    if system == "Darwin":
        iface = interface or "en0"
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", iface],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "Current Wi-Fi Network:" in result.stdout:
            return result.stdout.split(": ", 1)[1].strip()

    elif system == "Linux":
        result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("yes:"):
                    return line.split(":", 1)[1]

    return None
