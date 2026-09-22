"""Authenticated, durable command polling independent of capture/inference."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from cryptography.fernet import Fernet
from mantau_core.contracts import (
    CameraRef, CommandFailureReason, CommandResult, CommandState, CommandType,
    ControlCommand, Credentials, StreamProfile,
)

from .camera.validate import validate_camera
from .capabilities import InferenceMode
from .discovery.service import discover_cameras
from .state import CameraConfiguration, ConfigurationStore, SetupState
from .storage import atomic_write_json


class CompletedCommandStore:
    """Small durable result ledger; final results are written before upload."""

    def __init__(self, path: str | Path, *, secret: str = "", keep: int = 256) -> None:
        self.path = Path(path)
        self.keep = keep
        self._cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())) \
            if secret else None

    def get(self, command_id: str) -> CommandResult | None:
        values = self._read()
        raw = values.get(command_id)
        if not raw:
            return None
        if raw.get("kind") == "completed":
            raw = raw["result"]
        if raw.get("kind") == "inflight":
            return None
        return CommandResult.model_validate(raw)

    def get_inflight(self, command_id: str) -> ControlCommand | None:
        raw = self._read().get(command_id)
        if not raw or raw.get("kind") != "inflight":
            return None
        if "encrypted_command" in raw:
            if self._cipher is None:
                raise RuntimeError("command state encryption secret is unavailable")
            return ControlCommand.model_validate_json(
                self._cipher.decrypt(raw["encrypted_command"].encode("ascii")))
        return ControlCommand.model_validate(raw["command"])

    def put_inflight(self, command: ControlCommand) -> None:
        values = self._read()
        serialized = command.model_dump_json().encode("utf-8")
        if self._cipher is not None:
            values[command.command_id] = {
                "kind": "inflight",
                "encrypted_command": self._cipher.encrypt(serialized).decode("ascii"),
            }
        else:
            if any(key in command.payload for key in ("password", "credentials", "agent_secret")):
                raise RuntimeError("credential-bearing command state requires an encryption secret")
            values[command.command_id] = {
                "kind": "inflight", "command": command.model_dump(mode="json")}
        self._trim_and_write(values)

    def put(self, result: CommandResult) -> None:
        values = self._read()
        values[result.command_id] = {
            "kind": "completed", "result": result.model_dump(mode="json")}
        self._trim_and_write(values)

    def _trim_and_write(self, values: dict) -> None:
        while len(values) > self.keep:
            values.pop(next(iter(values)))
        atomic_write_json(self.path, values, mode=0o600)

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


class CommandExecutor:
    def __init__(self, settings, pipeline, *, config_store: ConfigurationStore | None = None,
                 restart_requested: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.config_store = config_store or ConfigurationStore()
        self.restart_requested = restart_requested or (lambda: None)

    def status(self) -> dict:
        health = self.pipeline.health()
        caps = health["capabilities"]
        return {
            "platform": caps["platform"],
            "capabilities": {
                "schema_version": 1,
                "platform": caps["platform"],
                "architecture": caps["architecture"],
                "cpu": caps["cpu"],
                "memory_bytes": caps.get("memory_bytes"),
                "available_accelerators": caps.get("available_accelerators", []),
                "supported_detector_backends": caps.get("supported_detector_backends", []),
                "software_version": caps["software_version"],
                "recommended_mode": caps["recommended_mode"],
                "supported_inference_modes": [mode.value for mode in InferenceMode],
                "recommendation_reason": caps.get("detector_error"),
            },
            "setup_status": "active",
            "health_state": "degraded" if health["routing"].get("degraded") else "online",
            "requested_inference_mode": self.settings.inference_mode.value,
            "effective_inference_mode": health["effective_inference_mode"],
            "camera_connectivity": "connected" if health["camera"]["connected"] else "disconnected",
            "health_explanation": health["routing"].get("reason"),
        }

    async def execute(self, command: ControlCommand) -> CommandResult:
        try:
            data, message = await self._execute(command)
            return CommandResult(command_id=command.command_id, state=CommandState.SUCCEEDED,
                                 message=message, data=data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception text can contain URLs or credentials from third-party
            # libraries. Only the type crosses the reporting/logging boundary.
            return CommandResult(
                command_id=command.command_id, state=CommandState.FAILED,
                failure_reason=CommandFailureReason.EXECUTION_FAILED,
                message=f"Command failed ({type(exc).__name__}).",
            )

    async def _execute(self, command: ControlCommand) -> tuple[dict, str]:
        if command.command_type is CommandType.DISCOVER:
            template = self._camera(command.payload)
            candidates = await discover_cameras(template, profile=StreamProfile.SUB)
            cameras = [{
                "schema_version": 1, "host": item.host, "name": None, "port": 554,
                "main_path": "/stream1", "sub_path": None,
                "rtsp_reachable": item.rtsp_reachable,
                "failure_reason": item.reachability_error,
            } for item in candidates]
            return {"cameras": cameras}, "Discovery completed."
        if command.command_type in (CommandType.CAMERA_TEST, CommandType.CONFIGURE_CAMERA):
            camera = self._camera(command.payload)
            profile = StreamProfile.SUB if command.payload.get("sub_path") else StreamProfile.MAIN
            failure = await validate_camera(camera, profile=profile)
            if failure:
                raise RuntimeError("camera validation failed")
            if command.command_type is CommandType.CONFIGURE_CAMERA:
                current = self.config_store.load()
                if current is None:
                    raise RuntimeError("agent configuration is unavailable")
                configured = CameraConfiguration(
                    camera_id=command.payload.get("camera_id", "cam-1"),
                    host=command.payload["host"], port=command.payload.get("port", 554),
                    main_path=command.payload.get("main_path", "/stream1"),
                    sub_path=command.payload.get("sub_path"),
                    username=command.payload.get("username"),
                    password=command.payload.get("password"),
                    default_stream_profile="sub" if command.payload.get("sub_path") else "main",
                )
                self.config_store.save(current.model_copy(update={
                    "camera": configured, "setup_state": SetupState.COMPLETE,
                }))
                self.restart_requested()
                return {}, "Camera configuration saved; restart requested."
            return {"success": True}, "Camera connection succeeded."
        if command.command_type is CommandType.SET_INFERENCE_MODE:
            mode = InferenceMode(command.payload["mode"])
            await self.pipeline.change_mode(mode)
            current = self.config_store.load()
            if current is not None:
                self.config_store.save(current.model_copy(update={"inference_mode": mode}))
            self.settings.inference_mode = mode
            return {"effective_mode": self.pipeline.router.mode.value}, "Inference mode updated."
        if command.command_type in (CommandType.RESTART, CommandType.RECONFIGURE):
            self.restart_requested()
            return {"restart_requested": True}, "Service restart requested."
        raise ValueError("unsupported command")

    def _camera(self, payload: dict) -> CameraRef:
        paths = {StreamProfile.MAIN: payload.get("main_path", "/stream1")}
        if payload.get("sub_path"):
            paths[StreamProfile.SUB] = payload["sub_path"]
        credentials = None
        if payload.get("username") is not None:
            credentials = Credentials(username=payload["username"], password=payload.get("password", ""))
        return CameraRef(
            camera_id=payload.get("camera_id", self.settings.camera_id),
            name=payload.get("name", payload.get("camera_id", self.settings.camera_id)),
            host=payload.get("host", self.settings.camera_host),
            port=payload.get("port", self.settings.camera_port), paths=paths,
            credentials=credentials,
        )


class ControlPlaneWorker:
    def __init__(self, server_url: str, agent_id: str, secret: str, executor: CommandExecutor,
                 state: CompletedCommandStore, *, poll_interval_s: float = 5.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self.executor = executor
        self.state = state
        self.poll_interval_s = poll_interval_s
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Mantau-Agent-ID": self.agent_id, "X-Mantau-Agent-Secret": self._secret}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                response = await self._client.post(
                    f"{self.server_url}/agent-control/commands/poll",
                    headers=self._headers, json={"status": self.executor.status()},
                )
                if response.status_code == 200:
                    await self._handle(ControlCommand.model_validate(response.json()))
                elif response.status_code not in (204, 404, 503):
                    response.raise_for_status()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, command: ControlCommand) -> None:
        completed = self.state.get(command.command_id)
        if completed is not None:
            await self._submit(completed)
            return
        # Persist the complete delivered command before acknowledging it. This
        # transfers custody of one-use credentials to the agent's private
        # state before the server erases its encrypted copy.
        command = self.state.get_inflight(command.command_id) or command
        self.state.put_inflight(command)
        await self._submit(CommandResult(command_id=command.command_id, state=CommandState.RUNNING,
                                         completed_at=None))
        result = await self.executor.execute(command)
        self.state.put(result)
        await self._submit(result)

    async def _submit(self, result: CommandResult) -> None:
        response = await self._client.post(
            f"{self.server_url}/agent-control/commands/{result.command_id}/results",
            headers=self._headers, json=result.model_dump(mode="json"),
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
