"""Probing the host: Tailscale identity, TLS certificates, and CUDA.

These all shell out to tools that may be absent (``tailscale``, ``openssl``,
``nvidia-smi``, ``systemctl``). Every function here degrades to None/False
rather than raising, so setup keeps working on a host that has none of them.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


def detect_tailscale_info() -> Tuple[Optional[str], Optional[str]]:
    """
    Detect Tailscale DNS name and IPv4 address.

    Returns:
        (dns_name, ip) tuple. dns_name is the MagicDNS hostname (e.g. "myhost.tail1234.ts.net"),
        ip is the Tailscale IPv4 address (e.g. "100.64.1.5").
        Either or both may be None if Tailscale is not available.
    """
    dns_name = None
    ip = None

    # Get MagicDNS name from tailscale status --json
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            raw_dns = status.get("Self", {}).get("DNSName", "")
            # DNSName has trailing dot, strip it
            if raw_dns:
                dns_name = raw_dns.rstrip(".")
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        pass

    # Get IPv4 address as fallback
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return dns_name, ip


def generate_tailscale_certs(certs_dir: str) -> bool:
    """
    Generate trusted TLS certificates via Tailscale.

    Uses `tailscale cert` to obtain certs signed by the Tailscale CA, which are
    automatically trusted on devices in the same tailnet. Tries without sudo first
    (works when the Tailscale operator is set to the current user via
    `tailscale set --operator=$USER`); falls back to `sudo` otherwise.

    Args:
        certs_dir: Directory to write server.crt and server.key into.

    Returns:
        True if certificates were generated successfully, False otherwise.
    """
    dns_name, _ = detect_tailscale_info()
    if not dns_name:
        return False

    certs_path = Path(certs_dir)
    certs_path.mkdir(parents=True, exist_ok=True)

    cert_file = certs_path / "server.crt"
    key_file = certs_path / "server.key"

    cert_cmd = [
        "tailscale",
        "cert",
        "--cert-file",
        str(cert_file),
        "--key-file",
        str(key_file),
        dns_name,
    ]

    try:
        # Try without sudo first (operator configured). Fall back to non-interactive
        # sudo (-n avoids hanging on a password prompt in unattended contexts).
        result = subprocess.run(cert_cmd, capture_output=True, text=True, timeout=30)
        used_sudo = False
        if result.returncode != 0:
            result = subprocess.run(
                ["sudo", "-n", *cert_cmd], capture_output=True, text=True, timeout=30
            )
            used_sudo = True
        if result.returncode != 0:
            return False

        if used_sudo:
            # sudo wrote the files as root; fix ownership so Docker (and our user) can read them
            uid = os.getuid()
            gid = os.getgid()
            subprocess.run(
                ["sudo", "-n", "chown", f"{uid}:{gid}", str(cert_file), str(key_file)],
                capture_output=True,
                timeout=10,
            )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def cert_needs_renewal(certs_dir: str, within_days: int = 21) -> bool:
    """
    Check whether the TLS cert in certs_dir is missing or expiring soon.

    Cheap, local-only check (no network): inspects the cert file's expiry via
    `openssl x509 -checkend`, which exits 0 if the cert is valid beyond the window
    and non-zero if it expires within it (or is already expired).

    Args:
        certs_dir: Directory containing server.crt.
        within_days: Treat the cert as needing renewal if it expires within this many days.

    Returns:
        True if the cert is missing or expires within `within_days`, False otherwise.
    """
    cert_file = Path(certs_dir) / "server.crt"
    if not cert_file.exists():
        return True

    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-checkend",
                str(within_days * 86400),
                "-noout",
                "-in",
                str(cert_file),
            ],
            capture_output=True,
            timeout=10,
        )
        return result.returncode != 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # If expiry can't be determined, err toward renewing.
        return True


def ensure_tailscale_cert(certs_dir: str, within_days: int = 21) -> Optional[bool]:
    """
    Renew the Tailscale TLS cert only if it is missing or near expiry.

    No-op in the common case: just checks the local cert file's expiry and returns
    without contacting Tailscale unless renewal is actually due. This keeps the
    expensive `tailscale cert` call (and Let's Encrypt issuance) to roughly once
    per certificate lifetime regardless of how often it is invoked.

    Args:
        certs_dir: Directory containing/receiving server.crt and server.key.
        within_days: Renew if the cert expires within this many days.

    Returns:
        None if no renewal was needed, True if renewed successfully,
        False if renewal was needed but failed.
    """
    if not cert_needs_renewal(certs_dir, within_days):
        return None
    return generate_tailscale_certs(certs_dir)


def tailscale_socket_path() -> Optional[str]:
    """
    Return the path to the local tailscaled Unix socket if present, else None.

    Used to decide whether Caddy can manage the Tailscale TLS cert itself (socket
    mounted into the container) or whether we must fall back to a host-issued cert
    file (e.g. Docker Desktop on macOS, where the socket isn't reachable from the VM).
    """
    for path in (
        "/var/run/tailscale/tailscaled.sock",
        "/run/tailscale/tailscaled.sock",
    ):
        if Path(path).exists():
            return path
    return None


def tailscaled_enabled_at_boot() -> Optional[bool]:
    """
    Whether the tailscaled systemd unit is enabled to start on boot.

    Returns:
        True  — `systemctl is-enabled tailscaled` reports enabled (it'll come back
                after a reboot).
        False — the unit exists but is disabled/static (started manually only;
                won't survive a reboot — the classic "Tailscale gone after reboot").
        None  — can't tell: not Linux, no systemd/systemctl, or no tailscaled unit
                (e.g. Tailscale.app on macOS, or a non-systemd init). Nothing to offer.
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "tailscaled"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    state = result.stdout.strip()
    if state == "enabled":
        return True
    # "disabled" / "static" → won't auto-start. Unknown unit → systemctl prints
    # nothing to stdout (message goes to stderr, rc=1) → treat as "can't tell".
    if state in ("disabled", "static"):
        return False
    return None


def enable_tailscaled_at_boot() -> bool:
    """
    Enable (and start) the tailscaled unit so it survives reboots.

    Runs `sudo systemctl enable --now tailscaled`. Returns True on success. Uses sudo
    because enabling a system unit needs root; the user may be prompted for a password.
    """
    try:
        return (
            subprocess.run(
                ["sudo", "systemctl", "enable", "--now", "tailscaled"]
            ).returncode
            == 0
        )
    except OSError:
        return False


def decide_cert_mode(server_address: str) -> str:
    """
    Decide how the HTTPS certificate is managed for the given server address.

    Returns:
        "static" — host issues the cert file and Caddy serves it. Only for a Tailscale
            (*.ts.net) address when no tailscaled socket is available to mount into
            Caddy (e.g. Docker Desktop on macOS). Renewed by the services.py startup hook.
        "caddy"  — Caddy obtains and auto-renews the cert itself: *.ts.net via the
            mounted tailscaled socket, a real domain via Let's Encrypt, and an IP or
            localhost via Caddy's internal CA. No host cert file, no renewal cron.
    """
    if server_address.endswith(".ts.net") and not tailscale_socket_path():
        return "static"
    return "caddy"


def detect_cuda_version(default: str = "cu126") -> str:
    """
    Detect system CUDA version from nvidia-smi output.

    Parses "CUDA Version: X.Y" from nvidia-smi and maps to PyTorch CUDA version strings.

    Args:
        default: Default CUDA version if detection fails (default: "cu126")

    Returns:
        PyTorch CUDA version string: "cu126" or "cu128"
    """
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", result.stdout)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                if (major, minor) >= (12, 8):
                    return "cu128"
                # cu126 is the lowest supported build (torch>=2.7 dropped cu121)
                return "cu126"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return default
