import os

import pytest

from mantau_agent.capabilities import InferenceMode
from mantau_agent.config import load_settings
from mantau_agent.state import (
    AgentConfiguration, CameraConfiguration, ConfigurationStore,
    CorruptConfigurationError, EnrollmentConfiguration, SetupState,
)


def configuration(*, secret="agent-secret", host="192.0.2.10", state=SetupState.COMPLETE):
    return AgentConfiguration(
        setup_state=state,
        enrollment=EnrollmentConfiguration(
            server_url="https://server.example", agent_id="agent-1", agent_secret=secret),
        camera=CameraConfiguration(
            camera_id="camera-1", host=host, sub_path="/low", username="camera-user",
            password="camera-secret", default_stream_profile="sub",
        ) if state is SetupState.COMPLETE else None,
        inference_mode=InferenceMode.HYBRID,
    )


def test_configuration_round_trip_and_settings_translation(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.save(configuration())
    loaded = store.load()
    assert loaded == configuration()
    settings, durable = load_settings(store.path)
    assert durable == loaded
    assert settings.agent_id == "agent-1"
    assert settings.camera_host == "192.0.2.10"
    assert settings.camera_sub_path == "/low"
    assert settings.default_stream_profile == "sub"
    assert settings.inference_mode is InferenceMode.HYBRID


def test_environment_overrides_durable_configuration(monkeypatch, tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.save(configuration())
    monkeypatch.setenv("MANTAU_CAMERA_HOST", "198.51.100.7")
    monkeypatch.setenv("MANTAU_INFERENCE_MODE", "EDGE")
    settings, _ = load_settings(store.path)
    assert settings.camera_host == "198.51.100.7"
    assert settings.inference_mode is InferenceMode.EDGE


def test_corruption_is_detected_without_falling_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"schema_version":1,"agent_secret":"truncated', encoding="utf-8")
    with pytest.raises(CorruptConfigurationError, match="corrupt"):
        ConfigurationStore(path).load()


def test_previous_valid_configuration_can_be_restored(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    first = configuration(host="192.0.2.1")
    second = configuration(host="192.0.2.2")
    store.save(first)
    store.save(second)
    assert store.load() == second
    assert store.restore_backup() == first
    assert store.load() == first


def test_secrets_are_excluded_from_model_representations():
    text = repr(configuration())
    assert "agent-secret" not in text
    assert "camera-secret" not in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_configuration_and_directory_permissions_are_private(tmp_path):
    path = tmp_path / "private" / "config.json"
    ConfigurationStore(path).save(configuration())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_enrolled_state_is_valid_but_still_requires_camera_setup(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    enrolled = configuration(state=SetupState.ENROLLED)
    store.save(enrolled)
    assert store.load().setup_state is SetupState.ENROLLED
    assert store.load().camera is None
