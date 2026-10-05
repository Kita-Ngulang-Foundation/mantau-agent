"""The pure factory functions in main.py -- `run()` itself is integration
glue (real signal handling, an indefinite loop) and isn't unit tested; see
that module's docstring.
"""

import sys

import pytest
from mantau_core.contracts import StreamProfile

from mantau_agent.config import Settings
import pytest
from mantau_agent.main import _build_camera, _build_tunnel
from mantau_agent.uplink.tunnel import NullTunnel, TailscaleTunnel


def test_build_camera_with_credentials_and_sub_stream():
    settings = Settings(camera_id="cam-1", camera_host="192.168.1.42",
                        camera_sub_path="/stream2", camera_username="admin",
                        camera_password="admin123")
    camera = _build_camera(settings)
    assert camera.stream_url(StreamProfile.SUB) == "rtsp://admin:admin123@192.168.1.42:554/stream2"


def test_build_camera_without_credentials():
    settings = Settings(camera_id="cam-1", camera_host="192.168.1.50")
    camera = _build_camera(settings)
    assert camera.credentials is None


def test_build_tunnel_null_by_default():
    assert isinstance(_build_tunnel(Settings()), NullTunnel)


def test_build_tunnel_tailscale_when_configured():
    assert isinstance(_build_tunnel(Settings(tunnel_provider="tailscale")), TailscaleTunnel)


def test_installed_agent_requires_https_and_does_not_embed_credentials():
    for url in ('http://server.local', 'https://user:password@server.local', 'https:///missing-host'):
        with pytest.raises(ValueError, match='HTTPS'):
            Settings(require_https=True, server_url=url)
    assert Settings(require_https=True, server_url='https://server.local').require_https
    assert Settings(server_url='http://localhost:8100').server_url.startswith('http://')
