"""The pure/injectable pieces of the first-run setup wizard --
`_pick_camera_interactive()` and `run_wizard()` itself are interactive
terminal glue (real `input()` calls) and aren't unit tested here, matching
this repo's own convention for I/O-bound entry points (see
test_main_factories.py's docstring).
"""

from __future__ import annotations

import json

import httpx
import pytest

import mantau_agent.setup_wizard as wizard
from mantau_agent.capabilities import InferenceMode
from mantau_agent.config import Settings
from mantau_agent.discovery.service import DiscoveryCandidate
from mantau_agent.state import ConfigurationStore, SetupState
from mantau_agent.setup_wizard import (
    _choose_host,
    default_agent_id,
    enroll,
    needs_setup,
    render_env_file,
)


def test_needs_setup_true_when_nothing_configured():
    assert needs_setup(Settings()) is True


def test_needs_setup_false_once_all_three_required_fields_are_set():
    settings = Settings(agent_id="agent-1", agent_secret="shh", camera_host="192.168.1.42")
    assert needs_setup(settings) is False


def test_needs_setup_true_if_only_some_fields_are_set():
    # A half-configured agent (e.g. agent_id set but never actually enrolled)
    # must still trigger setup rather than crash later with an empty secret.
    settings = Settings(agent_id="agent-1", camera_host="192.168.1.42")
    assert needs_setup(settings) is True


def test_default_agent_id_is_prefixed_and_lowercase():
    agent_id = default_agent_id()
    assert agent_id.startswith("agent-")
    assert agent_id == agent_id.lower()


def test_configured_discovery_does_not_require_manual_host_for_setup():
    assert not needs_setup(Settings(agent_id="agent", agent_secret="secret",
                                    use_onvif_discovery=True))


def test_render_env_file_includes_the_required_keys():
    text = render_env_file(
        server_url="https://server.example",
        agent_id="agent-1",
        secret="shh",
        camera={"host": "192.168.1.42", "port": "554", "main_path": "/stream1"},
    )
    assert "MANTAU_SERVER_URL=https://server.example" in text
    assert "MANTAU_AGENT_ID=agent-1" in text
    assert "MANTAU_AGENT_SECRET=shh" in text
    assert "MANTAU_CAMERA_HOST=192.168.1.42" in text
    assert "MANTAU_CAMERA_SUB_PATH" not in text
    assert "MANTAU_CAMERA_USERNAME" not in text


def test_render_env_file_includes_sub_stream_and_credentials_when_given():
    text = render_env_file(
        server_url="https://server.example",
        agent_id="agent-1",
        secret="shh",
        camera={
            "host": "192.168.1.42", "port": "554", "main_path": "/stream1",
            "sub_path": "/stream2", "username": "admin", "password": "admin123",
        },
    )
    assert "MANTAU_CAMERA_SUB_PATH=/stream2" in text
    assert "MANTAU_DEFAULT_STREAM_PROFILE=sub" in text
    assert "MANTAU_CAMERA_USERNAME=admin" in text
    assert "MANTAU_CAMERA_PASSWORD=admin123" in text


def test_enroll_sends_the_key_and_returns_the_secret():
    seen = []

    def handler(request):
        assert request.url.path == "/agents/enroll"
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"agent_id": "agent-1", "secret": "fresh-secret"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    secret = enroll("http://server.local:8100", "MTU-AAAAA-BBBBB-CCCCC-DDDDD", "agent-1",
                    name="Ruang tamu", client=client)
    assert secret == "fresh-secret"
    assert seen[0]["enrollment_key"] == "MTU-AAAAA-BBBBB-CCCCC-DDDDD"
    assert seen[0]["agent_id"] == "agent-1"
    assert seen[0]["name"] == "Ruang tamu"
    assert seen[0]["platform"] in {"linux_x86_64", "linux_arm64", "raspberry_pi", "android", "other"}


def test_a_rejected_key_says_how_to_get_a_new_one():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"detail": "invalid_enrollment_key"})))
    with pytest.raises(wizard.EnrollmentKeyRejected, match="Tambah perangkat"):
        enroll("http://server.local:8100", "MTU-OLD", "agent-1", client=client)


def test_unattended_remote_setup_enrolls_without_a_terminal(monkeypatch, tmp_path, capsys):
    store = ConfigurationStore(tmp_path / "config.json")
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected prompt"))
    mock = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={
        "agent_id": "x", "secret": "private-agent-value",
    })))
    monkeypatch.setattr(wizard, "enroll_device",
                        lambda url, key, name=None, client=None: _enroll_with(url, key, name, mock))
    configuration = wizard.run_wizard(store, remote=True, server_url="https://server",
                                      enrollment_key="MTU-KEY", name="Ruang tamu")
    from mantau_agent.config import load_settings
    settings, saved = load_settings(store.path)
    assert configuration.camera is None
    assert settings.command_channel_enabled
    assert settings.agent_secret == "private-agent-value"
    assert not needs_setup(settings, saved)
    output = capsys.readouterr().out
    assert "private-agent-value" not in output and "MTU-KEY" not in output


def _enroll_with(url, key, name, client):
    from mantau_agent.state import EnrollmentConfiguration
    secret = enroll(url, key, "agent-generated", name=name, client=client)
    return EnrollmentConfiguration(server_url=url, agent_id="agent-generated", agent_secret=secret)


def test_enroll_raises_on_an_http_error_instead_of_returning_garbage():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        enroll("http://server.local:8100", "MTU-KEY", "agent-1", client=client)
        assert False, "expected an HTTPStatusError"
    except httpx.HTTPStatusError:
        pass


def test_multiple_discoveries_require_an_explicit_nondefault_selection(monkeypatch):
    answers = iter(["", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    candidates = [
        DiscoveryCandidate(host="camera-one", rtsp_reachable=True),
        DiscoveryCandidate(host="camera-two", rtsp_reachable=True),
    ]
    assert _choose_host(candidates) == "camera-two"


def test_single_discovery_can_be_selected_without_a_choice_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected prompt"))
    assert _choose_host([
        DiscoveryCandidate(host="only-camera", rtsp_reachable=False,
                           reachability_error="auth_failed")
    ]) == "only-camera"


async def test_camera_setup_prefers_and_validates_configured_substream(monkeypatch):
    async def discover(*args, **kwargs):
        return []

    validated = []

    async def validate(camera, *, profile):
        validated.append((camera, profile))
        return None

    answers = iter(["192.0.2.10", "554", "/main", "/low", "camera-user"])
    monkeypatch.setattr(wizard, "discover_cameras", discover)
    monkeypatch.setattr(wizard, "validate_camera", validate)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt: "camera-password")
    camera = await wizard._pick_camera_interactive("agent")
    assert camera.default_stream_profile == "sub"
    assert camera.sub_path == "/low"
    assert camera.password == "camera-password"
    assert validated[0][1].value == "sub"


def test_interrupted_setup_persists_enrollment_for_safe_resume(monkeypatch, tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    answers = iter(["https://server", "MTU-KEY", "Ruang tamu"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wizard, "enroll", lambda *args, **kwargs: "agent-secret")

    async def cancelled(agent_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "_pick_camera_interactive", cancelled)
    with pytest.raises(RuntimeError, match="saved enrollment"):
        wizard.run_wizard(store)
    saved = store.load()
    assert saved.setup_state is SetupState.ENROLLED
    assert saved.enrollment.agent_secret == "agent-secret"


def test_enrollment_failure_does_not_expose_http_error_details(monkeypatch, tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    answers = iter(["https://server.invalid", "MTU-KEY", "Ruang tamu"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def fail(*args, **kwargs):
        raise httpx.ConnectError("agent-secret=must-not-appear")

    monkeypatch.setattr(wizard, "enroll", fail)
    with pytest.raises(RuntimeError) as caught:
        wizard.run_wizard(store)
    assert str(caught.value) == "Enrollment failed: ConnectError"
    assert "must-not-appear" not in str(caught.value)
    assert store.load() is None


def test_completed_setup_persists_camera_and_inference_mode(monkeypatch, tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    answers = iter(["https://server", "MTU-KEY", "Ruang tamu"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wizard, "enroll", lambda *args, **kwargs: "agent-secret")

    async def selected(agent_id):
        from mantau_agent.state import CameraConfiguration
        return CameraConfiguration(camera_id="cam", host="192.0.2.20")

    monkeypatch.setattr(wizard, "_pick_camera_interactive", selected)
    completed = wizard.run_wizard(store)
    assert completed.setup_state is SetupState.COMPLETE
    assert completed.inference_mode is InferenceMode.CLOUD
    assert store.load().camera.host == "192.0.2.20"


def test_default_agent_ids_do_not_collide_between_identical_devices():
    assert wizard.default_agent_id() != wizard.default_agent_id()


def test_a_taken_generated_id_is_retried_with_a_new_one():
    ids = []

    def handler(request):
        body = json.loads(request.content)
        ids.append(body["agent_id"])
        if len(ids) == 1:
            return httpx.Response(409, json={"detail": "agent_id_taken"})
        return httpx.Response(201, json={"agent_id": body["agent_id"], "secret": "s"})

    enrollment = wizard.enroll_device(
        "https://server/", "MTU-KEY", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(set(ids)) == 2
    assert enrollment.agent_id == ids[1]
    assert enrollment.server_url == "https://server"
