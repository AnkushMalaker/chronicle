from pathlib import Path

from chronicle_setup.compose import update_caddy_socket_override
from ruamel.yaml import YAML


def _load(path: Path):
    yaml = YAML(typ="safe")
    return yaml.load(path.read_text())


def test_caddy_socket_override_preserves_unrelated_local_services(tmp_path):
    override_path = tmp_path / "docker-compose.override.yml"
    override_path.write_text("""\
# Operator-owned local overrides.
services:
  caddy:
    image: caddy:2-alpine
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock
  vault-syncthing:
    volumes:
      - ../../machine-local/example-vault:/example-vault
""")

    mounted = update_caddy_socket_override(
        override_path,
        socket_path="/var/run/tailscale/tailscaled.sock",
        enabled=True,
    )

    assert mounted is True
    document = _load(override_path)
    assert document["services"]["vault-syncthing"]["volumes"] == [
        "../../machine-local/example-vault:/example-vault"
    ]
    assert document["services"]["caddy"]["image"] == "caddy:2-alpine"
    assert document["services"]["caddy"]["volumes"] == [
        "./Caddyfile:/etc/caddy/Caddyfile:ro",
        "/var/run/tailscale:/var/run/tailscale:ro",
    ]
    assert override_path.read_text().startswith("# Operator-owned local overrides.")


def test_disabling_caddy_socket_removes_only_managed_mount(tmp_path):
    override_path = tmp_path / "docker-compose.override.yml"
    override_path.write_text("""\
services:
  caddy:
    volumes:
      - ./extra-caddy:/srv/extra:ro
      - /run/tailscale:/run/tailscale:ro
  local-tool:
    volumes:
      - ./private:/private
""")

    mounted = update_caddy_socket_override(
        override_path,
        socket_path=None,
        enabled=False,
    )

    assert mounted is False
    assert _load(override_path) == {
        "services": {
            "caddy": {"volumes": ["./extra-caddy:/srv/extra:ro"]},
            "local-tool": {"volumes": ["./private:/private"]},
        }
    }


def test_disabling_last_generated_override_removes_empty_file(tmp_path):
    override_path = tmp_path / "docker-compose.override.yml"
    override_path.write_text("""\
services:
  caddy:
    volumes:
      - /var/run/tailscale:/var/run/tailscale:ro
""")

    mounted = update_caddy_socket_override(
        override_path,
        socket_path=None,
        enabled=False,
    )

    assert mounted is False
    assert not override_path.exists()


def test_enabling_caddy_socket_creates_read_only_directory_mount(tmp_path):
    override_path = tmp_path / "docker-compose.override.yml"

    mounted = update_caddy_socket_override(
        override_path,
        socket_path="/run/tailscale/tailscaled.sock",
        enabled=True,
    )

    assert mounted is True
    assert _load(override_path) == {
        "services": {"caddy": {"volumes": ["/run/tailscale:/run/tailscale:ro"]}}
    }
