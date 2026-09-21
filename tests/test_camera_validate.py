import numpy as np
from mantau_core.contracts import (
    AttemptedFrom, CameraRef, ReachabilityError, ReachabilityErrorKind, StreamProfile,
)

from mantau_agent.camera.validate import validate_camera


CAMERA = CameraRef(
    camera_id="cam", name="cam", host="camera",
    paths={StreamProfile.MAIN: "/main", StreamProfile.SUB: "/low"},
)


async def test_validation_requires_a_decoded_frame_and_uses_requested_substream(monkeypatch):
    profiles = []

    async def probe(camera, *, profile, timeout_s):
        profiles.append(profile)
        return None

    def read(camera, profile, timeout_s):
        profiles.append(profile)
        return True

    monkeypatch.setattr("mantau_agent.camera.validate.probe_camera_on_lan", probe)
    assert await validate_camera(CAMERA, profile=StreamProfile.SUB, frame_reader=read) is None
    assert profiles == [StreamProfile.SUB, StreamProfile.SUB]


async def test_validation_reports_probe_classification_without_credentials(monkeypatch):
    async def probe(camera, *, profile, timeout_s):
        return ReachabilityError(
            camera_id="cam", kind=ReachabilityErrorKind.AUTH_FAILED,
            attempted_from=AttemptedFrom.LAN, detail="unauthorized")

    monkeypatch.setattr("mantau_agent.camera.validate.probe_camera_on_lan", probe)
    failure = await validate_camera(
        CAMERA, profile=StreamProfile.MAIN,
        frame_reader=lambda camera, profile, timeout: False)
    assert failure == "RTSP validation failed (auth_failed)"


async def test_successful_authenticated_stream_is_authoritative_even_if_options_is_rejected(monkeypatch):
    async def probe(camera, *, profile, timeout_s):
        return ReachabilityError(
            camera_id="cam", kind=ReachabilityErrorKind.AUTH_FAILED,
            attempted_from=AttemptedFrom.LAN, detail="challenge")

    monkeypatch.setattr("mantau_agent.camera.validate.probe_camera_on_lan", probe)
    assert await validate_camera(
        CAMERA, profile=StreamProfile.MAIN,
        frame_reader=lambda camera, profile, timeout: True) is None
