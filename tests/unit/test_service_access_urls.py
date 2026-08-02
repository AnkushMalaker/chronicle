"""Canonical browser URLs for host-managed services."""

from pathlib import Path

import services


def _install_demo_service(monkeypatch, tmp_path: Path, *, marker: str = "ui:3000"):
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    monkeypatch.setitem(
        services.SERVICES,
        "demo",
        {
            "path": "demo",
            "ui": {
                "http_port": "3002",
                "http_port_env": "DEMO_UI_PORT",
                "https_port": "3443",
                "https_caddyfile": "proxy/Caddyfile",
                "https_marker": marker,
            },
        },
    )
    (tmp_path / "demo").mkdir()
    (tmp_path / "proxy").mkdir()


def test_service_ui_url_falls_back_to_configured_http_port(monkeypatch, tmp_path):
    _install_demo_service(monkeypatch, tmp_path)
    (tmp_path / "demo" / ".env").write_text("DEMO_UI_PORT=6123\n")

    assert services.service_ui_url("demo", "node.example.ts.net") == (
        "http://node.example.ts.net:6123"
    )


def test_service_ui_url_only_uses_https_when_expected_route_exists(
    monkeypatch, tmp_path
):
    _install_demo_service(monkeypatch, tmp_path)
    caddyfile = tmp_path / "proxy" / "Caddyfile"
    caddyfile.write_text("reverse_proxy some-other-service:3000\n")
    assert services.service_ui_url("demo", "node.example.ts.net") == (
        "http://node.example.ts.net:3002"
    )

    caddyfile.write_text("reverse_proxy ui:3000\n")
    assert services.service_ui_url("demo", "node.example.ts.net") == (
        "https://node.example.ts.net:3443"
    )


def test_service_ui_url_wraps_ipv6_hosts(monkeypatch, tmp_path):
    _install_demo_service(monkeypatch, tmp_path)

    assert services.service_ui_url("demo", "fd7a:115c:a1e0::1") == (
        "http://[fd7a:115c:a1e0::1]:3002"
    )
