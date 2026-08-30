import json
import subprocess

import pytest

import clients


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("state = running\npid = 42\n", True),
        ("state = spawn scheduled\nactive count = 0\n", False),
    ],
)
def test_macos_component_active_requires_a_running_job(monkeypatch, output, expected):
    monkeypatch.setattr(clients, "IS_MACOS", True)
    monkeypatch.setattr(clients, "_launchctl", lambda *args: completed(stdout=output))

    assert clients.component_active("screenpipe") is expected


def test_desktop_process_match_excludes_cli_collector_and_mcp(monkeypatch):
    processes = """\
  10 /Applications/screenpipe - Development.app/Contents/MacOS/screenpipe-app --autostart
  11 /usr/bin/screenpipe record --api-auth true
  12 node /tmp/node_modules/.bin/screenpipe-mcp
  13 chronicle-screenpipe run
"""
    monkeypatch.setattr(
        clients.subprocess,
        "run",
        lambda *args, **kwargs: completed(stdout=processes),
    )

    assert clients._screenpipe_desktop_processes() == [
        {
            "pid": 10,
            "command": (
                "/Applications/screenpipe - Development.app/Contents/MacOS/"
                "screenpipe-app --autostart"
            ),
        }
    ]


def test_runtime_reports_desktop_owner_and_meeting_warning(monkeypatch):
    monkeypatch.setattr(
        clients,
        "_screenpipe_desktop_processes",
        lambda: [{"pid": 17, "command": "screenpipe-app"}],
    )
    monkeypatch.setattr(clients, "component_active", lambda name: False)
    monkeypatch.setattr(clients, "_screenpipe_port_open", lambda: True)
    monkeypatch.setattr(clients, "_desktop_meeting_detection_disabled", lambda: True)

    status = clients.screenpipe_runtime_status()

    assert status == {
        "runtime_owner": "desktop_app",
        "recording_active": True,
        "conflict": False,
        "detail": "desktop app owns recording — meeting detection disabled",
        "desktop_pids": [17],
    }


def test_runtime_reports_unknown_port_owner(monkeypatch):
    monkeypatch.setattr(clients, "_screenpipe_desktop_processes", lambda: [])
    monkeypatch.setattr(clients, "component_active", lambda name: False)
    monkeypatch.setattr(clients, "_screenpipe_port_open", lambda: True)

    status = clients.screenpipe_runtime_status()

    assert status["runtime_owner"] == "unknown"
    assert status["conflict"] is True
    assert "unrecognized" in status["detail"]


def test_runtime_forwards_recorder_owned_vision_capture_status(monkeypatch):
    vision = {
        "requested": True,
        "state": "failed",
        "detail": "screen-share approval was cancelled or rejected",
    }
    monkeypatch.setattr(clients, "_screenpipe_desktop_processes", lambda: [])
    monkeypatch.setattr(clients, "_screenpipe_port_open", lambda: True)
    monkeypatch.setattr(
        clients, "_screenpipe_health", lambda: {"vision_capture": vision}
    )

    status = clients.screenpipe_runtime_status(chronicle_active=True)

    assert status["vision_capture"] == vision


def test_runtime_derives_failed_vision_capture_from_sustained_errors(monkeypatch):
    monkeypatch.setattr(clients, "_screenpipe_desktop_processes", lambda: [])
    monkeypatch.setattr(clients, "_screenpipe_port_open", lambda: True)
    monkeypatch.setattr(
        clients,
        "_screenpipe_health",
        lambda: {
            "status": "healthy",
            "pipeline": {
                "capture_attempts": 12,
                "frames_dropped_error": 10,
                "frame_drop_rate": 0.9,
            },
            "vision_db_write_stalled": False,
        },
    )

    status = clients.screenpipe_runtime_status(chronicle_active=True)

    assert status["vision_capture"]["state"] == "failed"
    assert "share every requested monitor" in status["vision_capture"]["detail"]


def test_runtime_does_not_warn_during_vision_startup(monkeypatch):
    monkeypatch.setattr(clients, "_screenpipe_desktop_processes", lambda: [])
    monkeypatch.setattr(clients, "_screenpipe_port_open", lambda: True)
    monkeypatch.setattr(
        clients,
        "_screenpipe_health",
        lambda: {
            "pipeline": {
                "capture_attempts": 2,
                "frames_dropped_error": 2,
                "frame_drop_rate": 1.0,
            },
            "vision_db_write_stalled": False,
        },
    )

    status = clients.screenpipe_runtime_status(chronicle_active=True)

    assert status["vision_capture"] is None


def test_desktop_meeting_setting_is_read_without_rewriting(tmp_path, monkeypatch):
    settings = tmp_path / "store.bin"
    settings.write_text(
        json.dumps({"settings": {"disableMeetingDetector": True, "other": 1}})
    )
    monkeypatch.setattr(clients, "_SCREENPIPE_SETTINGS", settings)

    assert clients._desktop_meeting_detection_disabled() is True
    assert json.loads(settings.read_text()) == {
        "settings": {"disableMeetingDetector": True, "other": 1},
    }


def test_macos_app_autostart_is_booted_out_and_disabled(tmp_path, monkeypatch):
    app_plist = tmp_path / "screenpipe.plist"
    calls = []
    monkeypatch.setattr(clients, "IS_MACOS", True)
    monkeypatch.setattr(
        clients,
        "_macos_screenpipe_app_agents",
        lambda: [("screenpipe - Development", app_plist)],
    )
    monkeypatch.setattr(
        clients,
        "_launchctl",
        lambda *args: calls.append(args) or completed(),
    )

    messages = clients.disable_screenpipe_app_autostart()

    assert calls == [
        ("bootout", f"gui/{clients.os.getuid()}", str(app_plist)),
        ("disable", f"gui/{clients.os.getuid()}/screenpipe - Development"),
    ]
    assert "remains manually launchable" in messages[0]


def test_linux_app_autostart_unit_is_masked(monkeypatch):
    calls = []
    unit = r"app-screenpipe\x20\x2d\x20Development@autostart.service"
    monkeypatch.setattr(clients, "IS_MACOS", False)
    monkeypatch.setattr(clients, "_linux_screenpipe_app_units", lambda: [unit])
    monkeypatch.setattr(
        clients,
        "_systemctl",
        lambda *args: calls.append(args) or completed(),
    )

    clients.disable_screenpipe_app_autostart()

    assert calls == [("mask", "--now", unit)]


def test_recorder_install_refuses_a_manually_running_desktop_app(monkeypatch):
    monkeypatch.setattr(clients, "disable_screenpipe_app_autostart", lambda: [])
    monkeypatch.setattr(
        clients,
        "_screenpipe_desktop_processes",
        lambda: [{"pid": 17, "command": "screenpipe-app"}],
    )

    with pytest.raises(RuntimeError, match="desktop app is running"):
        clients.install_component("screenpipe")


@pytest.mark.parametrize("action", ["start", "restart"])
def test_recorder_action_refuses_external_owner(monkeypatch, action):
    monkeypatch.setattr(
        clients,
        "screenpipe_runtime_status",
        lambda: {
            "runtime_owner": "desktop_app",
            "detail": "desktop app owns recording",
        },
    )

    with pytest.raises(RuntimeError, match="desktop app owns recording"):
        clients.component_action("screenpipe", action)


def test_reconcile_boots_out_retrying_macos_recorder(monkeypatch):
    actions = []
    monkeypatch.setattr(clients, "IS_MACOS", True)
    monkeypatch.setattr(
        clients,
        "_screenpipe_desktop_processes",
        lambda: [{"pid": 17, "command": "screenpipe-app"}],
    )
    monkeypatch.setattr(clients, "component_installed", lambda name: True)
    monkeypatch.setattr(clients, "_launchctl", lambda *args: completed())
    monkeypatch.setattr(
        clients,
        "component_action",
        lambda name, action: actions.append((name, action)) or True,
    )

    assert clients.reconcile_screenpipe_ownership() is True
    assert actions == [("screenpipe", "stop")]


def test_reconcile_never_stops_desktop_app_when_chronicle_is_unloaded(monkeypatch):
    monkeypatch.setattr(clients, "IS_MACOS", True)
    monkeypatch.setattr(
        clients,
        "_screenpipe_desktop_processes",
        lambda: [{"pid": 17, "command": "screenpipe-app"}],
    )
    monkeypatch.setattr(clients, "component_installed", lambda name: True)
    monkeypatch.setattr(clients, "_launchctl", lambda *args: completed(returncode=1))
    monkeypatch.setattr(
        clients,
        "component_action",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not stop app")),
    )

    assert clients.reconcile_screenpipe_ownership() is False
