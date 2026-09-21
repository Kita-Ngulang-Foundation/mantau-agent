"""One-frame RTSP validation used before setup becomes durable."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import cv2
from mantau_core.contracts import CameraRef, StreamProfile

from ..discovery.probe import probe_camera_on_lan


def _read_one_frame(camera: CameraRef, profile: StreamProfile, timeout_s: float) -> bool:
    timeout_ms = max(1, int(timeout_s * 1000))
    capture = cv2.VideoCapture(camera.stream_url(profile), cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms,
    ])
    try:
        if not capture.isOpened():
            return False
        ok, image = capture.read()
        return bool(ok and image is not None and getattr(image, "size", 0))
    finally:
        capture.release()


async def validate_camera(
    camera: CameraRef, *, profile: StreamProfile, timeout_s: float = 5.0,
    frame_reader: Callable[[CameraRef, StreamProfile, float], bool] = _read_one_frame,
) -> str | None:
    """Return None only after RTSP responds and a configured stream yields a frame."""
    probe_error = await probe_camera_on_lan(camera, profile=profile, timeout_s=timeout_s)
    frame_ok = await asyncio.to_thread(frame_reader, camera, profile, timeout_s)
    if frame_ok:
        return None
    if probe_error is not None:
        return f"RTSP validation failed ({probe_error.kind.value})"
    return "RTSP responded but the configured stream did not produce a frame"
