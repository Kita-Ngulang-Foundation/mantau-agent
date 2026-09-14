"""CameraPuller against an injected fake capture -- same approach as
mantau-backend-rtsp's RTSPPuller tests. Real background thread, so tests
poll for a short real-world duration rather than using an injectable clock.
"""

from __future__ import annotations

import time

import numpy as np
from mantau_core.contracts import CameraRef, StreamProfile
from mantau_core.resilience import BackoffPolicy

from mantau_agent.camera.puller import CameraPuller

FRAME = np.zeros((2, 2, 3), dtype=np.uint8)
CAMERA = CameraRef(camera_id="cam-1", name="Test", host="127.0.0.1",
                    paths={StreamProfile.SUB: "/stream2"})


def _wait_until(predicate, *, timeout=2.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeCapture:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames
        self._i = 0

    def read(self):
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return True, frame

    def release(self) -> None:
        pass


def test_produces_frames_from_a_working_capture():
    puller = CameraPuller(CAMERA, capture_factory=lambda: _FakeCapture([FRAME]))
    puller.start()
    try:
        assert _wait_until(lambda: puller.latest_frame() is not None)
        image, ts_ms = puller.latest_frame()
        assert image.shape == FRAME.shape
        assert puller.reachable is True
    finally:
        puller.stop()


def test_reconnects_after_open_failures_then_succeeds():
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("camera not answering yet")
        return _FakeCapture([FRAME])

    puller = CameraPuller(CAMERA, capture_factory=factory,
                          backoff=BackoffPolicy(base=0.001, cap=0.01))
    puller.start()
    try:
        assert _wait_until(lambda: puller.reachable is True)
        assert attempts["n"] >= 3
    finally:
        puller.stop()


def test_stop_interrupts_a_long_backoff_wait_promptly():
    def always_fails():
        raise ConnectionError("camera gone")

    puller = CameraPuller(CAMERA, capture_factory=always_fails,
                          backoff=BackoffPolicy(base=5.0, cap=30.0))
    puller.start()
    assert _wait_until(lambda: puller.restarts >= 1, timeout=1.0)

    t0 = time.monotonic()
    puller.stop()
    assert time.monotonic() - t0 < 1.0


def test_latest_frame_is_none_before_start():
    puller = CameraPuller(CAMERA, capture_factory=lambda: _FakeCapture([FRAME]))
    assert puller.latest_frame() is None
