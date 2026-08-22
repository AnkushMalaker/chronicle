"""Safe updates for machine-local Docker Compose overrides."""

from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

_TAILSCALE_MOUNT_TARGETS = {
    "/run/tailscale",
    "/run/tailscale/tailscaled.sock",
    "/var/run/tailscale",
    "/var/run/tailscale/tailscaled.sock",
}


def _mount_target(volume: Any) -> Optional[str]:
    """Return a Compose volume entry's container target when recognizable."""
    if isinstance(volume, str):
        parts = volume.split(":")
        return parts[1] if len(parts) >= 2 else None
    if isinstance(volume, Mapping):
        target = volume.get("target")
        return str(target) if target else None
    return None


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _write_yaml(path: Path, yaml: YAML, document: CommentedMap) -> None:
    stream = StringIO()
    yaml.dump(document, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stream.getvalue())


def update_caddy_socket_override(
    override_path: Path,
    *,
    socket_path: Optional[str],
    enabled: bool,
) -> bool:
    """Update only Caddy's managed Tailscale mount in a local override.

    Unrelated services, Caddy settings, comments, and volume mounts are retained.
    Disabling the mount removes the override file only when no user-owned content
    remains.

    Returns ``True`` when the directory mount is enabled, otherwise ``False``.
    """
    override_path = Path(override_path)
    yaml = _yaml()

    if override_path.exists():
        document = yaml.load(override_path.read_text())
        if document is None:
            document = CommentedMap()
        elif not isinstance(document, CommentedMap):
            raise ValueError(
                f"Compose override must be a YAML mapping: {override_path}"
            )
    else:
        document = CommentedMap()
        document.yaml_set_start_comment(
            "Machine-local Docker Compose overrides.\n"
            "The setup wizard owns only Caddy's Tailscale mount; other entries are preserved."
        )

    services = document.get("services")
    if services is None:
        if not enabled:
            return False
        services = CommentedMap()
        document["services"] = services
    elif not isinstance(services, CommentedMap):
        raise ValueError(
            f"Compose override services must be a YAML mapping: {override_path}"
        )

    caddy = services.get("caddy")
    if caddy is None:
        if not enabled:
            return False
        caddy = CommentedMap()
        services["caddy"] = caddy
    elif not isinstance(caddy, CommentedMap):
        raise ValueError(
            f"Compose override service 'caddy' must be a YAML mapping: {override_path}"
        )

    volumes = caddy.get("volumes")
    if volumes is None:
        volumes = CommentedSeq()
    elif not isinstance(volumes, list):
        raise ValueError(
            f"Compose override caddy.volumes must be a YAML list: {override_path}"
        )

    retained = CommentedSeq(
        volume
        for volume in volumes
        if _mount_target(volume) not in _TAILSCALE_MOUNT_TARGETS
    )
    changed = list(retained) != list(volumes)

    if enabled:
        if not socket_path:
            raise ValueError("socket_path is required when the Caddy mount is enabled")
        socket_dir = str(Path(socket_path).parent)
        retained.append(f"{socket_dir}:{socket_dir}:ro")
        caddy["volumes"] = retained
        _write_yaml(override_path, yaml, document)
        return True

    if retained:
        if changed:
            caddy["volumes"] = retained
    else:
        caddy.pop("volumes", None)

    if not caddy:
        services.pop("caddy", None)
    if not services:
        document.pop("services", None)

    if not document:
        override_path.unlink(missing_ok=True)
    elif changed:
        _write_yaml(override_path, yaml, document)

    return False
