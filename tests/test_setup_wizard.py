"""The pure/injectable pieces of the first-run setup wizard --
`_pick_camera_interactive()` and `run_wizard()` itself are interactive
terminal glue (real `input()` calls) and aren't unit tested here, matching
this repo's own convention for I/O-bound entry points (see
test_main_factories.py's docstring).
"""

from __future__ import annotations

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


def test_enroll_returns_the_secret_from_a_successful_response():
    def handler(request):
        assert request.url.path == "/agents/enroll"
        return httpx.Response(201, json={"agent_id": "agent-1", "secret": "fresh-secret"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    secret = enroll("http://server.local:8100", "agent-1", client=client)
    assert secret == "fresh-secret"


def test_enroll_raises_on_an_http_error_instead_of_returning_garbage():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        enroll("http://server.local:8100", "agent-1", client=client)
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
    answers = iter(["https://server", "agent-one"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wizard, "enroll", lambda *args: "agent-secret")

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
    answers = iter(["https://server.invalid", "agent-one"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def fail(*args):
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
    answers = iter(["https://server", "agent-one", "HYBRID"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wizard, "enroll", lambda *args: "agent-secret")

    async def selected(agent_id):
        from mantau_agent.state import CameraConfiguration
        return CameraConfiguration(camera_id="cam", host="192.0.2.20")

    monkeypatch.setattr(wizard, "_pick_camera_interactive", selected)
    completed = wizard.run_wizard(store)
    assert completed.setup_state is SetupState.COMPLETE
    assert completed.inference_mode is InferenceMode.HYBRID
    assert store.load().camera.host == "192.0.2.20"
