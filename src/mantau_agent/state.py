"""Versioned durable setup state with strict corruption detection."""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .capabilities import InferenceMode
from .storage import atomic_write_bytes, atomic_write_json


class SetupState(str, Enum):
    ENROLLED = "enrolled"
    COMPLETE = "complete"


class EnrollmentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: str
    agent_id: str
    agent_secret: str = Field(repr=False)


class CameraConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str
    host: str
    port: int = Field(default=554, ge=1, le=65535)
    main_path: str = "/stream1"
    sub_path: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    default_stream_profile: Literal["sub", "main"] = "main"


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    setup_state: SetupState
    enrollment: EnrollmentConfiguration
    camera: CameraConfiguration | None = None
    inference_mode: InferenceMode = InferenceMode.AUTO

    def settings_data(self) -> dict:
        values = {
            "server_url": self.enrollment.server_url,
            "agent_id": self.enrollment.agent_id,
            "agent_secret": self.enrollment.agent_secret,
            "inference_mode": self.inference_mode,
        }
        if self.camera is not None:
            values.update({
                "camera_id": self.camera.camera_id,
                "camera_host": self.camera.host,
                "camera_port": self.camera.port,
                "camera_main_path": self.camera.main_path,
                "camera_sub_path": self.camera.sub_path,
                "camera_username": self.camera.username,
                "camera_password": self.camera.password,
                "default_stream_profile": self.camera.default_stream_profile,
                "use_onvif_discovery": False,
            })
        return values


class ConfigurationError(RuntimeError):
    pass


class CorruptConfigurationError(ConfigurationError):
    pass


def default_config_path() -> Path:
    configured = os.environ.get("MANTAU_CONFIG_PATH")
    return Path(configured) if configured else Path("data/config.json")


class ConfigurationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")

    def load(self) -> AgentConfiguration | None:
        if not self.path.exists():
            return None
        try:
            return AgentConfiguration.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise CorruptConfigurationError(
                f"Configuration {self.path} is corrupt; repair it or restore {self.backup_path}"
            ) from exc

    def save(self, configuration: AgentConfiguration) -> None:
        validated = AgentConfiguration.model_validate(configuration)
        if self.path.exists():
            # Only preserve a known-good previous version as rollback material.
            current = self.load()
            if current is not None:
                atomic_write_bytes(self.backup_path, self.path.read_bytes(), mode=0o600)
        atomic_write_json(self.path, validated.model_dump(mode="json"), mode=0o600)

    def restore_backup(self) -> AgentConfiguration:
        try:
            configuration = AgentConfiguration.model_validate_json(self.backup_path.read_bytes())
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise CorruptConfigurationError(f"Backup {self.backup_path} is unavailable or corrupt") from exc
        atomic_write_json(self.path, configuration.model_dump(mode="json"), mode=0o600)
        return configuration
