import json

import pytest

from mantau_agent import main
from mantau_agent.capabilities import InferenceMode
from mantau_agent.health.status import StatusStore
from mantau_agent.state import (
    AgentConfiguration, CameraConfiguration, ConfigurationStore,
    EnrollmentConfiguration, SetupState,
)


def test_status_json_is_machine_readable_when_service_has_not_started(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MANTAU_STATUS_PATH", str(tmp_path / "missing-status.json"))
    code = main.cli(["--config", str(tmp_path / "missing.json"), "status", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["running"] is False
    assert output["configured"] is False
    assert output["version"]


def test_status_path_cli_option_reads_installed_snapshot(tmp_path, capsys):
    path = tmp_path / "installed-status.json"
    StatusStore(path).write({"running": False, "marker": "installed"})
    code = main.cli([
        "--config", str(tmp_path / "missing.json"),
        "--status-path", str(path), "status", "--json",
    ])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["marker"] == "installed"


def test_status_json_never_contains_configuration_secrets(monkeypatch, tmp_path, capsys):
    path = tmp_path / "status.json"
    StatusStore(path).write({
        "running": True, "pid": 99999999,
        "camera": {"connected": True}, "uplink": {"connected": True},
    })
    monkeypatch.setenv("MANTAU_STATUS_PATH", str(path))
    monkeypatch.setenv("MANTAU_AGENT_SECRET", "do-not-print-agent")
    monkeypatch.setenv("MANTAU_CAMERA_PASSWORD", "do-not-print-camera")
    code = main.cli(["--config", str(tmp_path / "missing.json"), "status", "--json"])
    text = capsys.readouterr().out
    output = json.loads(text)
    assert code == 0
    assert output["running"] is False
    assert output["camera"]["connected"] is False
    assert output["uplink"]["connected"] is False
    assert "do-not-print" not in text


def test_discover_json_returns_inventory_without_credentials(monkeypatch, tmp_path, capsys):
    async def discover(settings):
        return [{"host": "camera", "rtsp_reachable": True}]

    monkeypatch.setattr(main, "_discover_command", discover)
    monkeypatch.setenv("MANTAU_CAMERA_PASSWORD", "do-not-print")
    code = main.cli(["--config", str(tmp_path / "missing.json"), "discover", "--json"])
    text = capsys.readouterr().out
    assert code == 0
    assert json.loads(text) == [{"host": "camera", "rtsp_reachable": True}]
    assert "do-not-print" not in text


def test_corrupted_configuration_is_reported_without_secret_contents(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"agent_secret":"never-print-this"', encoding="utf-8")
    code = main.cli(["--config", str(path), "status", "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "corrupt" in captured.err
    assert "never-print-this" not in captured.err


def test_runtime_discovery_refuses_ambiguous_reachable_cameras(monkeypatch):
    from mantau_agent.config import Settings
    from mantau_agent.discovery.service import DiscoveryCandidate

    async def discover(*args, **kwargs):
        return [
            DiscoveryCandidate(host="one", rtsp_reachable=True),
            DiscoveryCandidate(host="two", rtsp_reachable=True),
        ]

    monkeypatch.setattr(main, "discover_cameras", discover)
    with pytest.raises(RuntimeError, match="Multiple reachable"):
        import asyncio
        asyncio.run(main._discover_camera(Settings(use_onvif_discovery=True)))


def test_restore_backup_exits_without_reentering_setup(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    store = ConfigurationStore(path)

    def configured(host):
        return AgentConfiguration(
            setup_state=SetupState.COMPLETE,
            enrollment=EnrollmentConfiguration(
                server_url="https://server", agent_id="agent", agent_secret="secret"),
            camera=CameraConfiguration(camera_id="camera", host=host),
            inference_mode=InferenceMode.AUTO,
        )

    first = configured("192.0.2.1")
    store.save(first)
    store.save(configured("192.0.2.2"))
    monkeypatch.setattr("mantau_agent.setup_wizard.run_wizard",
                        lambda *args: pytest.fail("setup should not run"))
    code = main.cli(["--config", str(path), "setup", "--restore-backup"])
    output = capsys.readouterr().out
    assert code == 0
    assert store.load() == first
    assert "secret" not in output


def test_process_permission_error_means_the_service_may_still_exist(monkeypatch):
    def denied(*args):
        raise PermissionError

    monkeypatch.setattr(main.os, "kill", denied)
    assert main._process_exists(1234)
