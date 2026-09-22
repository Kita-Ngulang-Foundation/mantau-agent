from __future__ import annotations

import json
from pathlib import Path

import httpx
import mantau_core
from mantau_core.contracts import CommandResult, CommandState, CommandType, ControlCommand

from mantau_agent import control
from mantau_agent.config import Settings
from mantau_agent.control import CommandExecutor, CompletedCommandStore, ControlPlaneWorker


class StubExecutor:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"health_state": "online"}

    async def execute(self, command):
        self.calls += 1
        return CommandResult(command_id=command.command_id, state=CommandState.SUCCEEDED,
                             message="done")


def test_python_agent_consumes_shared_core_v1_fixture():
    path = Path(mantau_core.__file__).parent / "contracts" / "fixtures" / "v1" / "command.json"
    command = ControlCommand.model_validate_json(path.read_text(encoding="utf-8"))
    assert command.command_type is CommandType.DISCOVER
    assert command.schema_version == 1


async def test_completed_command_is_not_reexecuted_after_process_restart(tmp_path):
    submitted = []

    def handler(request):
        submitted.append(json.loads(request.content))
        return httpx.Response(204)

    command = ControlCommand.model_validate({
        "schema_version": 1, "command_id": "cmd-once", "command_type": "restart",
        "state": "delivered", "payload": {}, "created_at": "2026-09-22T08:00:00Z",
        "expires_at": "2099-09-22T08:05:00Z",
    })
    path = tmp_path / "commands.json"
    executor = StubExecutor()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = ControlPlaneWorker("http://server", "agent", "secret", executor,
                                   CompletedCommandStore(path), client=client)
        await first._handle(command)
        restarted = ControlPlaneWorker("http://server", "agent", "secret", executor,
                                       CompletedCommandStore(path), client=client)
        await restarted._handle(command)

    assert executor.calls == 1
    assert [item["state"] for item in submitted] == ["running", "succeeded", "succeeded"]
    persisted = CompletedCommandStore(path).get("cmd-once")
    assert persisted is not None and persisted.state is CommandState.SUCCEEDED


async def test_inflight_credentials_survive_acknowledgement_restart_and_are_removed_on_completion(tmp_path):
    password = "one-use-camera-password"
    submitted = []

    def handler(request):
        submitted.append(json.loads(request.content))
        return httpx.Response(204)

    class CrashOnce(StubExecutor):
        async def execute(self, command):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated process exit")
            assert command.payload["password"] == password
            return CommandResult(command_id=command.command_id, state=CommandState.SUCCEEDED)

    command = ControlCommand(
        command_id="cmd-camera-recovery", command_type=CommandType.CAMERA_TEST,
        state=CommandState.DELIVERED,
        payload={"host": "192.0.2.10", "username": "operator", "password": password},
        created_at="2026-09-22T08:00:00Z", expires_at="2099-09-22T08:05:00Z",
    )
    path = tmp_path / "commands.json"
    executor = CrashOnce()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = ControlPlaneWorker("http://server", "agent", "secret", executor,
                                    CompletedCommandStore(path, secret="secret"), client=client)
        try:
            await worker._handle(command)
        except RuntimeError:
            pass
        redelivered_without_secret = command.model_copy(update={"payload": {"host": "192.0.2.10"}})
        await worker._handle(redelivered_without_secret)

    assert executor.calls == 2
    assert password not in path.read_text(encoding="utf-8")


async def test_restart_and_reconfigure_request_service_restart(tmp_path):
    requested = []
    executor = CommandExecutor(Settings(), object(), restart_requested=lambda: requested.append(True))
    for command_type in (CommandType.RESTART, CommandType.RECONFIGURE):
        command = ControlCommand(
            command_id=f"cmd-{command_type.value}", command_type=command_type,
            state=CommandState.DELIVERED, payload={},
            created_at="2026-09-22T08:00:00Z", expires_at="2099-09-22T08:05:00Z",
        )
        result = await executor.execute(command)
        assert result.state is CommandState.SUCCEEDED
        assert result.data == {"restart_requested": True}
    assert requested == [True, True]


async def test_camera_failure_never_returns_or_logs_credentials(monkeypatch, caplog):
    password = "do-not-leak-this-camera-password"

    async def fail(*args, **kwargs):
        raise RuntimeError(f"bad RTSP password {password}")

    monkeypatch.setattr(control, "validate_camera", fail)
    executor = CommandExecutor(Settings(), object())
    command = ControlCommand(
        command_id="cmd-camera", command_type=CommandType.CAMERA_TEST,
        state=CommandState.DELIVERED,
        payload={"camera_id": "cam-1", "name": "Room", "host": "192.0.2.10",
                 "main_path": "/main", "username": "operator", "password": password},
        created_at="2026-09-22T08:00:00Z", expires_at="2099-09-22T08:05:00Z",
    )
    result = await executor.execute(command)
    assert result.state is CommandState.FAILED
    assert password not in result.model_dump_json()
    assert password not in caplog.text
