"""The pure factory functions in main.py -- `run()` itself is integration
glue (real signal handling, an indefinite loop) and isn't unit tested; see
that module's docstring.
"""

import pytest
from mantau_core.contracts import StreamProfile
from mantau_core.detection import NullDetector

from mantau_agent.config import Settings
from mantau_agent.main import _build_camera, _build_detector, _build_tunnel
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


def test_build_detector_defaults_to_null():
    settings = Settings()
    detector = _build_detector(settings)
    assert isinstance(detector, NullDetector)


def test_build_detector_mediapipe_fails_loudly_without_mantau_ai():
    settings = Settings(detector_backend="mediapipe")
    with pytest.raises(ImportError, match="streaming entrypoint"):
        _build_detector(settings)


def test_build_tunnel_null_by_default():
    assert isinstance(_build_tunnel(Settings()), NullTunnel)


def test_build_tunnel_tailscale_when_configured():
    assert isinstance(_build_tunnel(Settings(tunnel_provider="tailscale")), TailscaleTunnel)
