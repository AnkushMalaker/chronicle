"""Host-health checks: container DNS, Tailscale state, TLS, and socket mounts.

These cover faults that leave Chronicle's own services reporting healthy while the
deployment is unusable. Mongo and Redis are container-local, so a host that has lost
external DNS or its TLS certificate still passes every existing health check.

Like :mod:`chronicle_setup.system`, every function here degrades rather than raising:
a missing ``tailscale``/``openssl``/engine binary, or a container that is not running,
yields ``NOT_APPLICABLE`` instead of an error. That distinction is load-bearing —
it lets the same suite run honestly in CI (where nothing is configured) and on a
fully configured host, without skipping tests or inventing failures.

Checks never mutate anything. A check that has a known idempotent remedy exposes it
as ``CheckResult.repair``, a zero-argument callable the caller may choose to invoke.
"""

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

OK = "ok"
WARN = "warn"
FAIL = "fail"
NOT_APPLICABLE = "not_applicable"

# Exit code the shell reserves for "command not found". A probe that shells into a
# container hits this when the image lacks the tool, which says nothing about health.
_CMD_NOT_FOUND = 127


@dataclass
class CheckResult:
    """Outcome of a single check.

    ``repair`` is present only when the fault has a remedy that is safe to apply
    unattended and idempotent. Anything needing a human (an expired node key, a
    service down on another machine) carries a ``remedy`` string and no callable.
    """

    id: str
    title: str
    status: str
    detail: str = ""
    remedy: str = ""
    repair: Optional[Callable[[], bool]] = None

    @property
    def needs_attention(self) -> bool:
        return self.status in (WARN, FAIL)

    def as_dict(self) -> dict:
        """JSON-safe form. The repair callable becomes a boolean."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
            "repairable": self.repair is not None,
        }


@dataclass
class CheckContext:
    """What the checks need to know about this host.

    Supplied by the caller rather than discovered here, so the module stays free of
    imports from ``services.py`` (which imports this package) and stays trivially
    testable.
    """

    engine: str = "docker"
    # Containers to probe for DNS. The first one that is running is used.
    dns_containers: Tuple[str, ...] = ()
    dns_probe_host: str = "api.openai.com"
    # Containers that bind-mount the tailscaled socket.
    socket_containers: Tuple[str, ...] = ()
    socket_path: str = "/var/run/tailscale/tailscaled.sock"
    https_host: Optional[str] = None
    https_port: int = 443
    operator_user: Optional[str] = None
    warn_expiry_days: int = 14
    network: Optional[str] = None
    reload_image: str = "docker.io/library/alpine:latest"


def _run(argv: List[str], timeout: int = 15, stdin_text: Optional[str] = None):
    """Run a command, returning None if it could not be executed at all.

    None means "could not determine" (binary absent, OS refused); a returned
    CompletedProcess means the command ran, whatever its exit code.
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _container_running(engine: str, container: str) -> bool:
    result = _run(
        [engine, "inspect", "-f", "{{.State.Running}}", container], timeout=10
    )
    return bool(result and result.returncode == 0 and result.stdout.strip() == "true")


def _first_running(engine: str, containers: Tuple[str, ...]) -> Optional[str]:
    for name in containers:
        if _container_running(engine, name):
            return name
    return None


def _tailscale_status() -> Optional[dict]:
    """Parsed ``tailscale status --json``, or None if unavailable/unparseable."""
    if shutil.which("tailscale") is None:
        return None
    result = _run(["tailscale", "status", "--json"], timeout=10)
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


# --------------------------------------------------------------------------- checks


def check_container_dns(ctx: CheckContext) -> CheckResult:
    """Resolve an external name from inside a container.

    The failure this catches is specific: Podman's aardvark-dns can keep answering
    container-name lookups while silently refusing to forward anything external, so
    probing from the host proves nothing. It has to be asked from inside a container.
    """
    cid = "container_dns"
    title = "Container can resolve external DNS"

    if shutil.which(ctx.engine) is None:
        return CheckResult(cid, title, NOT_APPLICABLE, f"{ctx.engine} not installed")

    container = _first_running(ctx.engine, ctx.dns_containers)
    if container is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "no probe container is running")

    result = _run(
        [ctx.engine, "exec", container, "getent", "hosts", ctx.dns_probe_host],
        timeout=20,
    )
    if result is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "could not exec into container")
    if result.returncode == _CMD_NOT_FOUND:
        return CheckResult(cid, title, NOT_APPLICABLE, "getent absent in image")
    if result.returncode == 0 and result.stdout.strip():
        return CheckResult(cid, title, OK, f"{container} resolved {ctx.dns_probe_host}")

    return CheckResult(
        cid,
        title,
        FAIL,
        f"{container} cannot resolve {ctx.dns_probe_host}",
        remedy=(
            "Pin DNS upstreams with `dns:` in the compose file. As an immediate "
            "fix, add or remove any container on the network to force an "
            "aardvark-dns reload."
        ),
        repair=lambda: repair_container_dns(ctx),
    )


def check_container_magicdns(ctx: CheckContext) -> CheckResult:
    """Resolve this node's own MagicDNS name from inside a container.

    Not implied by ``check_container_dns``: public upstreams answer public names
    while every ``*.ts.net`` name fails, so both probes are needed. See
    docs/backend/compose-stack.md#dns-pinning-x-public-dns.
    """
    cid = "container_magicdns"
    title = "Container can resolve Tailscale MagicDNS"

    if shutil.which(ctx.engine) is None:
        return CheckResult(cid, title, NOT_APPLICABLE, f"{ctx.engine} not installed")

    status = _tailscale_status()
    if status is None or status.get("BackendState") != "Running":
        return CheckResult(cid, title, NOT_APPLICABLE, "Tailscale not running")

    probe = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not probe:
        return CheckResult(cid, title, NOT_APPLICABLE, "no MagicDNS name for this node")

    container = _first_running(ctx.engine, ctx.dns_containers)
    if container is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "no probe container is running")

    result = _run([ctx.engine, "exec", container, "getent", "hosts", probe], timeout=20)
    if result is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "could not exec into container")
    if result.returncode == _CMD_NOT_FOUND:
        return CheckResult(cid, title, NOT_APPLICABLE, "getent absent in image")
    if result.returncode == 0 and result.stdout.strip():
        return CheckResult(cid, title, OK, f"{container} resolved {probe}")

    return CheckResult(
        cid,
        title,
        FAIL,
        f"{container} cannot resolve {probe}",
        remedy=(
            "List 100.100.100.100 first in the compose file's `dns:` upstreams "
            "(x-public-dns), then recreate the containers — `dns:` is applied at "
            "create time, so a plain restart will not pick it up."
        ),
    )


def check_tailscale_login(ctx: CheckContext) -> CheckResult:
    """Whether tailscaled is actually logged in.

    A node whose key expired keeps the daemon running and the interface present, so
    only the backend state distinguishes it from a healthy node.
    """
    cid = "tailscale_login"
    title = "Tailscale is logged in"

    status = _tailscale_status()
    if status is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "Tailscale not available")

    state = status.get("BackendState") or "unknown"
    if state == "Running":
        name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
        return CheckResult(cid, title, OK, f"running as {name or 'unknown host'}")

    return CheckResult(
        cid,
        title,
        FAIL,
        f"BackendState is {state}",
        remedy=(
            "Run `tailscale login` (or `wsl -u root tailscale login` on WSL, which "
            "needs no sudo password) and approve the printed URL in a browser."
        ),
    )


def check_tailscale_key_expiry(ctx: CheckContext) -> CheckResult:
    """Warn before the node key expires.

    Deliberately has no repair: key expiry is a control-plane setting, so nothing on
    the node can extend it. The only durable fix is disabling key expiry for this
    machine in the Tailscale admin console.
    """
    cid = "tailscale_key_expiry"
    title = "Tailscale node key is not expiring soon"

    status = _tailscale_status()
    if status is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "Tailscale not available")

    raw = (status.get("Self") or {}).get("KeyExpiry")
    if not raw:
        # No expiry set is the desired end state, not a missing reading.
        return CheckResult(cid, title, OK, "key expiry is disabled for this node")

    expiry = _parse_rfc3339(raw)
    if expiry is None:
        return CheckResult(cid, title, NOT_APPLICABLE, f"unparseable KeyExpiry {raw!r}")

    days = (expiry - time.time()) / 86400
    if days <= 0:
        return CheckResult(
            cid,
            title,
            FAIL,
            "node key has expired",
            remedy="Re-authenticate with `tailscale login`, then disable key expiry.",
        )
    if days <= ctx.warn_expiry_days:
        return CheckResult(
            cid,
            title,
            WARN,
            f"node key expires in {days:.0f} days",
            remedy=(
                "Disable key expiry for this machine at "
                "https://login.tailscale.com/admin/machines — it cannot be changed "
                "from the node itself."
            ),
        )
    return CheckResult(cid, title, OK, f"expires in {days:.0f} days")


def check_tailscale_operator(ctx: CheckContext) -> CheckResult:
    """Whether a non-root operator may use the tailscaled API.

    Caddy fetches its ``*.ts.net`` certificate over the tailscaled socket as
    container-root, which rootless Podman maps to the host user. Without an operator
    set, tailscaled denies that with ``cert access denied`` and HTTPS silently stops
    serving. ``tailscale login`` clears this pref, so re-authenticating breaks TLS.
    """
    cid = "tailscale_operator"
    title = "Tailscale operator is set"

    if shutil.which("tailscale") is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "Tailscale not available")
    if not ctx.operator_user:
        return CheckResult(cid, title, NOT_APPLICABLE, "no expected operator given")

    result = _run(["tailscale", "debug", "prefs"], timeout=10)
    if result is None or result.returncode != 0:
        return CheckResult(cid, title, NOT_APPLICABLE, "could not read prefs")
    try:
        prefs = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return CheckResult(cid, title, NOT_APPLICABLE, "could not parse prefs")

    current = prefs.get("OperatorUser") or ""
    if current == ctx.operator_user:
        return CheckResult(cid, title, OK, f"operator is {current}")

    return CheckResult(
        cid,
        title,
        FAIL,
        f"operator is {current or 'unset'}, expected {ctx.operator_user}",
        remedy=f"Run `sudo tailscale set --operator={ctx.operator_user}`.",
        repair=lambda: repair_tailscale_operator(ctx.operator_user),
    )


def check_socket_mount_fresh(ctx: CheckContext) -> CheckResult:
    """Detect a container holding a deleted tailscaled socket.

    Bind-mounting a unix socket *file* pins an inode. systemd's
    ``RuntimeDirectory=tailscale`` deletes and recreates ``/run/tailscale`` whenever
    tailscaled restarts, so the container keeps a socket that no longer exists and
    every connection is refused. Comparing mtimes catches it; the container's copy
    stays frozen at the moment it was mounted.
    """
    cid = "socket_mount_fresh"
    title = "Mounted tailscaled socket is current"

    host_socket = Path(ctx.socket_path)
    if not host_socket.exists():
        return CheckResult(cid, title, NOT_APPLICABLE, "no tailscaled socket on host")
    if shutil.which(ctx.engine) is None:
        return CheckResult(cid, title, NOT_APPLICABLE, f"{ctx.engine} not installed")

    try:
        host_mtime = int(host_socket.stat().st_mtime)
    except OSError:
        return CheckResult(cid, title, NOT_APPLICABLE, "could not stat host socket")

    stale = []
    checked = 0
    for container in ctx.socket_containers:
        if not _container_running(ctx.engine, container):
            continue
        result = _run(
            [ctx.engine, "exec", container, "stat", "-c", "%Y", ctx.socket_path],
            timeout=20,
        )
        # A non-zero exit means the container does not mount the socket at all, so
        # it is simply out of scope. A genuinely stale mount still stats fine —
        # the deleted inode remains visible through the bind mount, frozen at the
        # mtime it had when the container started. That is the whole signal.
        if result is None or result.returncode != 0:
            continue
        try:
            container_mtime = int(result.stdout.strip())
        except ValueError:
            continue
        checked += 1
        if container_mtime != host_mtime:
            stale.append(container)

    if not checked and not stale:
        return CheckResult(cid, title, NOT_APPLICABLE, "no container mounts the socket")
    if not stale:
        return CheckResult(cid, title, OK, f"{checked} container(s) current")

    return CheckResult(
        cid,
        title,
        FAIL,
        f"stale socket in: {', '.join(stale)}",
        remedy=(
            "Restart the affected containers. To stop it recurring, mount the "
            "directory /var/run/tailscale instead of the socket file and set "
            "RuntimeDirectoryPreserve=yes on tailscaled."
        ),
        repair=lambda: repair_restart_containers(ctx.engine, tuple(stale)),
    )


def check_tls_cert(ctx: CheckContext) -> CheckResult:
    """Verify the TLS certificate actually served for the public hostname.

    Must use SNI. Caddy serves its own internal-CA certificate for ``localhost`` even
    while the real hostname is failing, so a bare connection to port 443 looks
    healthy during exactly the outage this is meant to catch.
    """
    cid = "tls_cert"
    title = "HTTPS serves a valid certificate"

    if not ctx.https_host:
        return CheckResult(cid, title, NOT_APPLICABLE, "HTTPS not configured")
    if shutil.which("openssl") is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "openssl not installed")

    s_client = _run(
        [
            "openssl",
            "s_client",
            "-connect",
            f"{ctx.https_host}:{ctx.https_port}",
            "-servername",
            ctx.https_host,
        ],
        timeout=25,
        stdin_text="",
    )
    if (
        s_client is None
        or s_client.returncode != 0
        or "BEGIN CERTIFICATE" not in (s_client.stdout or "")
    ):
        return CheckResult(
            cid,
            title,
            FAIL,
            f"no certificate served for {ctx.https_host}",
            remedy=(
                "Check the Caddy logs. `connection refused` on the tailscaled "
                "socket means a stale mount; `cert access denied` means the "
                "Tailscale operator is unset."
            ),
        )

    x509 = _run(
        [
            "openssl",
            "x509",
            "-noout",
            "-issuer",
            "-enddate",
            "-checkend",
            str(ctx.warn_expiry_days * 86400),
        ],
        timeout=15,
        stdin_text=s_client.stdout,
    )
    if x509 is None:
        return CheckResult(cid, title, NOT_APPLICABLE, "could not inspect certificate")

    detail = " ".join((x509.stdout or "").split())
    if x509.returncode != 0:
        return CheckResult(
            cid,
            title,
            WARN,
            f"certificate expires within {ctx.warn_expiry_days} days: {detail}",
            remedy="Confirm automatic renewal is working; see docs/ssl-certificates.md.",
        )
    return CheckResult(cid, title, OK, detail)


# -------------------------------------------------------------------------- repairs


def repair_container_dns(ctx: CheckContext) -> bool:
    """Force aardvark-dns to reload by churning a container on the network.

    aardvark only re-reads its upstream resolvers when its config file changes, which
    Podman rewrites on any container add/remove. Starting and immediately removing a
    throwaway container is the least invasive way to trigger that.
    """
    if not ctx.network:
        return False
    result = _run(
        [
            ctx.engine,
            "run",
            "--rm",
            "--network",
            ctx.network,
            ctx.reload_image,
            "true",
        ],
        timeout=120,
    )
    return bool(result and result.returncode == 0)


def repair_restart_containers(engine: str, containers: Tuple[str, ...]) -> bool:
    """Restart containers so their bind mounts are re-resolved.

    A plain restart is enough: the engine re-applies mounts on start, so the current
    socket is picked up without recreating the container or touching compose.
    """
    if not containers:
        return False
    ok = True
    for container in containers:
        result = _run([engine, "restart", container], timeout=120)
        ok = ok and bool(result and result.returncode == 0)
    return ok


def repair_tailscale_operator(user: Optional[str]) -> bool:
    """Re-assert the operator pref, falling back to non-interactive sudo.

    Mirrors ``generate_tailscale_certs``: try unprivileged first, since it succeeds
    whenever the pref is merely being re-applied, and only then reach for sudo.
    """
    if not user:
        return False
    argv = ["tailscale", "set", f"--operator={user}"]
    result = _run(argv, timeout=15)
    if result is not None and result.returncode == 0:
        return True
    result = _run(["sudo", "-n", *argv], timeout=15)
    return bool(result and result.returncode == 0)


# ------------------------------------------------------------------------- registry

ALL_CHECKS: Tuple[Callable[[CheckContext], CheckResult], ...] = (
    check_container_dns,
    check_container_magicdns,
    check_tailscale_login,
    check_tailscale_key_expiry,
    check_tailscale_operator,
    check_socket_mount_fresh,
    check_tls_cert,
)


def run_all_checks(
    ctx: CheckContext, only: Optional[Tuple[str, ...]] = None
) -> List[CheckResult]:
    """Run every check, never raising.

    A check that blows up is reported as ``NOT_APPLICABLE`` rather than taking the
    caller down — a broken probe must not be able to stop a service from starting.
    """
    results = []
    for check in ALL_CHECKS:
        try:
            result = check(ctx)
        except Exception as exc:  # noqa: BLE001 - a probe must never be fatal
            name = getattr(check, "__name__", "check")
            result = CheckResult(name, name, NOT_APPLICABLE, f"check error: {exc}")
        if only and result.id not in only:
            continue
        results.append(result)
    return results


def worst_status(results: List[CheckResult]) -> str:
    """Roll a set of results up to the most severe status present."""
    for status in (FAIL, WARN, OK):
        if any(r.status == status for r in results):
            return status
    return NOT_APPLICABLE


def _parse_rfc3339(value: str) -> Optional[float]:
    """Epoch seconds from an RFC3339 timestamp, or None.

    Tailscale emits ``2027-01-28T05:28:38Z``; Python's fromisoformat only learned to
    accept a trailing ``Z`` in 3.11, and this package supports 3.9.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Fractional seconds can exceed six digits, which fromisoformat rejects.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(digits) :] if len(tail) > len(digits) else ""
        for marker in ("+", "-"):
            if marker in tail:
                offset = tail[tail.index(marker) :]
                break
        text = f"{head}.{digits or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
