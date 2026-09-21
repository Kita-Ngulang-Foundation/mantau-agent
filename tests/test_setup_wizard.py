"""The pure/injectable pieces of the first-run setup wizard --
`_pick_camera_interactive()` and `run_wizard()` itself are interactive
terminal glue (real `input()` calls) and aren't unit tested here, matching
this repo's own convention for I/O-bound entry points (see
test_main_factories.py's docstring).
"""

from __future__ import annotations

import httpx

from mantau_agent.config import Settings
from mantau_agent.setup_wizard import (
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
