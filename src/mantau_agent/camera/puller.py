"""Pull frames from the camera, forever, reconnecting with real backoff.

Identical shape to mantau-backend-rtsp's `RTSPPuller` -- background thread
(cv2's `VideoCapture.read()` blocks), latest-frame-wins, an interruptible
backoff wait on `stop()`. Deliberately duplicated rather than shared; the
one difference worth knowing about is which network the reconnect loop is
actually fighting: there, the internet path to a customer's camera; here,
the LAN, which is a much more reliable connection to begin with -- if this
loop is restarting often, the problem is almost certainly the camera
itself, not the network.
"""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np
from mantau_core.contracts import CameraRef, StreamProfile
from mantau_core.resilience import BackoffPolicy


class CameraPuller:
    def __init__(
        self,
        camera: CameraRef,
        *,
        profile: StreamProfile = StreamProfile.SUB,
        backoff: BackoffPolicy | None = None,
        capture_factory=None,
    ) -> None:
        self.camera = camera
        self.profile = profile
        self._backoff = backoff or BackoffPolicy(base=0.5, cap=15.0)
        self._capture_factory = capture_factory or self._open_cv2

        self._lock = threading.Lock()
        self._latest: tuple[np.ndarray, int] | None = None
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._thread: threading.Thread | None = None

        self.reachable = False
        self.restarts = 0
        self.last_error: str | None = None
        self.last_frame_at: datetime | None = None

    def _open_cv2(self):
        url = self.camera.stream_url(self.profile)
        # Bound native open/read calls so stop() can join the capture thread.
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500,
        ])
        if not cap.isOpened():
            cap.release()
            raise ConnectionError(f"could not open RTSP stream for camera {self.camera.camera_id!r}")
        return cap

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"camera-puller-{self.camera.camera_id}"
        )
        self._thread.start()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._capture_factory()
                self.reachable = True
                attempt = 0
                while not self._stop.is_set():
                    ok, image = cap.read()
                    if not ok:
                        raise ConnectionError("frame read failed")
                    ts_ms = int((time.monotonic() - self._t0) * 1000)
                    with self._lock:
                        self._latest = (image, ts_ms)
                        self.last_frame_at = datetime.now(timezone.utc)
            except Exception as exc:  # noqa: BLE001 -- this IS the reconnect boundary
                self.reachable = False
                # Native capture exceptions can echo the RTSP URL, including
                # embedded credentials. Keep health useful without persisting
                # or logging third-party exception text.
                self.last_error = type(exc).__name__
                if cap is not None:
                    with contextlib.suppress(Exception):
                        cap.release()
                    cap = None
                if self._stop.is_set():
                    break
                self.restarts += 1
                delay = self._backoff.delay_for(attempt)
                # Once the configured cap has been reached, retaining more
                # exponent bits cannot change the delay and can eventually
                # overflow in a months-long outage.
                attempt = min(attempt + 1, 63)
                self._stop.wait(delay)
            finally:
                if cap is not None:
                    with contextlib.suppress(Exception):
                        cap.release()
        self.reachable = False

    def latest_frame(self) -> tuple[np.ndarray, int] | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise TimeoutError("Camera capture did not stop within 2 seconds")
            self._thread = None
