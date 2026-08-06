"""Retired recorder flags are swept from the per-node spec on update.

The recorder's argv is per-node state that code updates never regenerate, so a
flag removed from the recommended invocation (currently
``--disable-meeting-detector``) must be migrated out by ``restart_installed``'s
sweep or it survives forever.
"""

import json

import clients


def _spec(tmp_path, argv):
    spec_dir = tmp_path / "clients"
    spec_dir.mkdir()
    (spec_dir / "screenpipe.json").write_text(
        json.dumps({"argv": argv, "env": {"SCREENPIPE_API_KEY": "k"}})
    )
    return spec_dir


def test_retired_flag_is_dropped_and_recorder_restarted(tmp_path, monkeypatch):
    spec_dir = _spec(
        tmp_path,
        ["screenpipe", "record", "--disable-meeting-detector", "--disable-telemetry"],
    )
    monkeypatch.setattr(clients, "_SPEC_DIR", spec_dir)
    monkeypatch.setattr(clients, "component_installed", lambda name: True)
    installs, actions = [], []
    monkeypatch.setattr(
        clients, "install_component", lambda name: installs.append(name)
    )
    monkeypatch.setattr(
        clients, "component_action", lambda name, action: actions.append((name, action))
    )

    assert clients.migrate_recorder_spec() is True

    saved = json.loads((spec_dir / "screenpipe.json").read_text())
    assert saved["argv"] == ["screenpipe", "record", "--disable-telemetry"]
    assert saved["env"] == {"SCREENPIPE_API_KEY": "k"}
    assert installs == ["screenpipe"]
    assert actions == [("screenpipe", "restart")]


def test_clean_spec_is_left_alone(tmp_path, monkeypatch):
    spec_dir = _spec(tmp_path, ["screenpipe", "record", "--disable-telemetry"])
    monkeypatch.setattr(clients, "_SPEC_DIR", spec_dir)
    monkeypatch.setattr(clients, "component_installed", lambda name: True)
    monkeypatch.setattr(
        clients,
        "component_action",
        lambda name, action: (_ for _ in ()).throw(AssertionError),
    )

    assert clients.migrate_recorder_spec() is False


def test_node_without_a_recorder_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "_SPEC_DIR", tmp_path / "clients")
    monkeypatch.setattr(clients, "component_installed", lambda name: False)

    assert clients.migrate_recorder_spec() is False
