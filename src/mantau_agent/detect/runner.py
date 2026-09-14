"""Poll the puller for new frames, sample down to a rate the hardware can
actually run detection at, run them through a `Detector`, hand off any
events to `on_event`.

Unlike mantau-backend-rtsp's `DetectorWorker`, this never persists or
dispatches anything itself -- `on_event` is wired by `main.py` to the uplink
client (build an envelope, send-or-spool). The agent has no local database;
relaying is its whole job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mantau_core.contracts import FallEvent
from mantau_core.detection import Detector

from .sampler import FrameSampler


class DetectRunner:
    def __init__(
        self,
        puller,  # CameraPuller -- untyped so this stays importable without cv2 in unit tests
        detector: Detector,
        on_event: Callable[[FallEvent], Awaitable[None]],
        *,
        sampler: FrameSampler | None = None,
        poll_interval_s: float = 0.02,
    ) -> None:
        self.puller = puller
        self.detector = detector
        self.on_event = on_event
        self.sampler = sampler or FrameSampler()
        self.poll_interval_s = poll_interval_s
        self._last_ts_ms: int | None = None

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            frame_info = self.puller.latest_frame()
            if frame_info is not None:
                image, ts_ms = frame_info
                if ts_ms != self._last_ts_ms:
                    self._last_ts_ms = ts_ms
                    if self.sampler.should_keep(ts_ms):
                        events = await asyncio.to_thread(self.detector.push, image, ts_ms)
                        for event in events:
                            await self.on_event(event)
            await asyncio.sleep(self.poll_interval_s)
