"""Platform-independent routing; one bounded queue and worker per frame path."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from mantau_core.detection import Detector, NullDetector

from ..capabilities import CapabilityReport, InferenceMode, select_inference_mode
from ..uplink.frames import FrameUplink
from ..uplink.inference import InferenceUplink
from .sampler import FrameSampler


@dataclass
class FrameWork:
    image: object
    ts_ms: int
    event_ids: tuple[str, ...] = ()


class InferenceRouter:
    """Owns detector and frame transports; caller owns capture/event uplink.

    EDGE sends events only (including suppressing live view). CLOUD skips the
    detector. HYBRID confirms only real local events, with a strict cap on
    upload attempts. Missing cloud transport is visible in health, never routed
    to the live-view endpoint. All frame queues drop oldest under pressure.
    """

    def __init__(self, *, camera_id: str, detector: Detector | None,
                 event_uplink, frame_uplink: FrameUplink,
                 capabilities: CapabilityReport, mode: InferenceMode = InferenceMode.AUTO,
                 inference_uplink: InferenceUplink | None = None,
                 sampler: FrameSampler | None = None, detection_fps: float = 5.0,
                 cloud_upload_fps: float = 1.0, confirmation_fps: float = 0.2,
                 live_view_fps: float = 4.0, live_view_enabled: bool = True,
                 queue_size: int = 2, upload_timeout_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        rates = (detection_fps, cloud_upload_fps, confirmation_fps, live_view_fps,
                 upload_timeout_s)
        if queue_size < 1 or any(not math.isfinite(r) or r <= 0 for r in rates):
            raise ValueError("Queue size and all rates/timeouts must be positive and finite")
        self.camera_id = camera_id
        self.detector = detector
        self.event_uplink = event_uplink
        self.frame_uplink = frame_uplink
        self.inference_uplink = inference_uplink
        self.capabilities = capabilities
        self.selection = select_inference_mode(mode, capabilities, detection_fps=detection_fps)
        self.detection_fps = detection_fps
        self.cloud_upload_fps = cloud_upload_fps
        self.confirmation_fps = confirmation_fps
        self.live_view_fps = live_view_fps
        self.live_view_enabled = live_view_enabled
        self.upload_timeout_s = upload_timeout_s
        self._clock = clock
        self._sampler = sampler or FrameSampler()
        self._detection_sampler = FrameSampler(max_fps=detection_fps)
        self._cloud_sampler = FrameSampler(max_fps=cloud_upload_fps)
        self._live_sampler = FrameSampler(max_fps=live_view_fps)
        self._queues = {name: asyncio.Queue(maxsize=queue_size)
                        for name in ("detection", "cloud", "live")}
        self.dropped = {name: 0 for name in self._queues}
        self.upload_failures = {"cloud": 0, "live": 0}
        self.last_error: str | None = None
        self.synthetic_events_suppressed = 0
        self._detector_ok = detector is not None and not isinstance(detector, NullDetector)
        self._last_attempt: dict[str, float] = {}
        self._last_ts: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._accepting = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def mode(self) -> InferenceMode:
        return self.selection.mode

    @property
    def detector_alive(self) -> bool:
        return self._running and self.mode != InferenceMode.CLOUD and self._detector_ok

    def health(self) -> dict:
        return {
            "mode": self.mode.value, "reason": self.selection.reason,
            "running": self._running, "detector_alive": self.detector_alive,
            "cloud_available": self.inference_uplink is not None,
            "degraded": ((self.mode != InferenceMode.EDGE and self.inference_uplink is None)
                         or (self.mode != InferenceMode.CLOUD and not self._detector_ok)),
            "queue_depths": {k: q.qsize() for k, q in self._queues.items()},
            "dropped_frames": dict(self.dropped), "upload_failures": dict(self.upload_failures),
            "synthetic_events_suppressed": self.synthetic_events_suppressed,
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Router is closed; create a new instance")
            if self._running:
                return
            self._running = self._accepting = True
            self._tasks = [asyncio.create_task(self._worker(name), name=f"router-{name}")
                           for name in self._queues]

    def _enqueue(self, name: str, work: FrameWork) -> None:
        queue = self._queues[name]
        if queue.full():
            queue.get_nowait()
            queue.task_done()
            self.dropped[name] += 1
        queue.put_nowait(work)

    def submit(self, image, ts_ms: int) -> None:
        """Nonblocking capture handoff; duplicate/stale timestamps are ignored."""
        if not self._accepting or (self._last_ts is not None and ts_ms <= self._last_ts):
            return
        self._last_ts = ts_ms
        work = FrameWork(image, ts_ms)
        if self.mode != InferenceMode.CLOUD:
            if self._sampler.should_keep(ts_ms) and self._detection_sampler.should_keep(ts_ms):
                self._enqueue("detection", work)
        elif self.inference_uplink is not None and self._cloud_sampler.should_keep(ts_ms):
            self._enqueue("cloud", work)
        if (self.mode != InferenceMode.EDGE and self.live_view_enabled
                and self._live_sampler.should_keep(ts_ms)):
            self._enqueue("live", work)

    async def _worker(self, name: str) -> None:
        queue = self._queues[name]
        while True:
            work = await queue.get()
            try:
                if work is None:
                    return
                if name == "detection":
                    await self._detect(work)
                else:
                    await self._upload(name, work)
            except Exception as exc:
                # Boundary around third-party detector/transport implementations.
                self.last_error = f"{name}: {type(exc).__name__}"
                if name == "detection":
                    self._detector_ok = False
                else:
                    self.upload_failures[name] += 1
            finally:
                queue.task_done()

    async def _detect(self, work: FrameWork) -> None:
        if self.detector is None:
            return
        events = await asyncio.to_thread(self.detector.push, work.image, work.ts_ms)
        self._detector_ok = not isinstance(self.detector, NullDetector)
        production = []
        for event in events:
            if isinstance(self.detector, NullDetector) or event.signals.get("synthetic"):
                self.synthetic_events_suppressed += 1
                continue
            await self.event_uplink.send_event(event)
            production.append(event.event_id)
        if (production and self.mode == InferenceMode.HYBRID
                and self.inference_uplink is not None and self._accepting):
            self._enqueue("cloud", FrameWork(work.image, work.ts_ms, tuple(production)))

    async def _upload(self, name: str, work: FrameWork) -> None:
        rate = (self.live_view_fps if name == "live" else
                min(self.cloud_upload_fps, self.confirmation_fps)
                if self.mode == InferenceMode.HYBRID else self.cloud_upload_fps)
        now = self._clock()
        if name in self._last_attempt and now - self._last_attempt[name] + 1e-9 < 1.0 / rate:
            self.dropped[name] += 1
            return
        jpeg = await asyncio.to_thread(self.frame_uplink.encode, work.image)
        if jpeg is None:
            self.upload_failures[name] += 1
            return
        # Measure from the actual send, after potentially slow JPEG encoding.
        # Failures and mode transitions do not reset this attempt budget.
        self._last_attempt[name] = self._clock()
        if name == "live":
            request = self.frame_uplink.push(jpeg)
        else:
            request = self.inference_uplink.submit(
                jpeg, camera_id=self.camera_id, ts_ms=work.ts_ms, event_ids=work.event_ids)
        if not await asyncio.wait_for(request, timeout=self.upload_timeout_s):
            self.upload_failures[name] += 1

    def _discard_pending(self) -> None:
        for name, queue in self._queues.items():
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
                self.dropped[name] += 1

    async def wait_idle(self) -> None:
        # Detection can enqueue confirmations, so drain it before cloud.
        for queue in self._queues.values():
            await queue.join()

    async def change_mode(self, mode: InferenceMode) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Router is closed")
            selection = select_inference_mode(mode, self.capabilities,
                                               detection_fps=self.detection_fps)
            self._accepting = False
            self._discard_pending()
            # A completed change guarantees no old-mode calls remain in flight.
            await self.wait_idle()
            self.selection = selection
            self._accepting = self._running

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._accepting = False
            self._discard_pending()
            # Never close a detector while its synchronous push is still running.
            await self.wait_idle()
            if self._tasks:
                for queue in self._queues.values():
                    queue.put_nowait(None)
            await asyncio.gather(*self._tasks)
            self._running = False
            self._closed = True
            try:
                if self.detector is not None:
                    await asyncio.to_thread(self.detector.close)
            finally:
                try:
                    await self.frame_uplink.close()
                finally:
                    if self.inference_uplink is not None:
                        await self.inference_uplink.close()
