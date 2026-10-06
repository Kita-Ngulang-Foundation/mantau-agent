"""Platform-independent CLOUD routing; one bounded queue and worker per frame path."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from mantau_core.activity import ActivityEngine

from ..capabilities import CapabilityReport, InferenceMode, select_inference_mode
from ..uplink.frames import FrameUplink
from ..uplink.inference import InferenceUplink
from .sampler import FrameSampler


@dataclass
class FrameWork:
    image: object
    ts_ms: int
    captured_at_ms: int | None = None


class InferenceRouter:
    """Owns the frame transports; the caller owns capture and the event uplink.

    The agent never detects on device: sampled frames go to server inference
    (CLOUD), whatever mode is stored or requested. Without server inference the
    agent detects nothing and health reports degraded; frames are never routed
    to the live-view endpoint instead. All frame queues drop oldest under
    pressure.
    """

    def __init__(self, *, camera_id: str, frame_uplink: FrameUplink,
                 capabilities: CapabilityReport, mode: InferenceMode = InferenceMode.AUTO,
                 inference_uplink: InferenceUplink | None = None,
                 sampler: FrameSampler | None = None, cloud_upload_fps: float = 15.0,
                 live_view_fps: float = 4.0, live_view_enabled: bool = True,
                 queue_size: int = 2, upload_timeout_s: float = 5.0,
                 activity: ActivityEngine | None = None, clips=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        rates = (cloud_upload_fps, live_view_fps, upload_timeout_s)
        if queue_size < 1 or any(not math.isfinite(r) or r <= 0 for r in rates):
            raise ValueError("Queue size and all rates/timeouts must be positive and finite")
        self.camera_id = camera_id
        self.frame_uplink = frame_uplink
        self.inference_uplink = inference_uplink
        self.capabilities = capabilities
        self.requested_mode = InferenceMode(mode)
        self.selection = select_inference_mode(mode, capabilities)
        self.cloud_upload_fps = cloud_upload_fps
        self.live_view_fps = live_view_fps
        self.live_view_enabled = live_view_enabled
        self.upload_timeout_s = upload_timeout_s
        # The server runs the activity rules on the frames it receives. The
        # agent keeps the engine for the saved per-camera settings, camera
        # outage handling and health reporting.
        self.activity = activity or ActivityEngine()
        self.clips = clips
        self._clock = clock
        self._sampler = sampler or FrameSampler()
        self._cloud_sampler = FrameSampler(max_fps=cloud_upload_fps)
        self._live_sampler = FrameSampler(max_fps=live_view_fps)
        self._queues = {name: asyncio.Queue(maxsize=queue_size) for name in ("cloud", "live")}
        self.dropped = {name: 0 for name in self._queues}
        self.upload_failures = {"cloud": 0, "live": 0}
        self.last_error: str | None = None
        self._last_attempt: dict[str, float] = {}
        self._live_slots = asyncio.Semaphore(3)
        self._live_tasks: set[asyncio.Task] = set()
        self._last_ts: int | None = None
        self._capture_offset_ms = None
        self._last_processed = None
        self._last_processed_at = None
        self._last_capture = None
        self._cloud_error = None
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
        """No detector runs on the agent."""
        return False

    def health(self) -> dict:
        fresh = (self._last_processed is not None and self._last_capture is not None
                 and self._clock()-min(self._last_processed,self._last_capture) <= 15
                 and self._cloud_error is None)
        return {
            "mode": self.mode.value, "requested_mode": self.requested_mode.value,
            "reason": self.selection.reason,
            "running": self._running, "detector_alive": self.detector_alive,
            "cloud_available": fresh,
            "last_inference_at": self._last_processed_at,
            "degraded": not fresh,
            "queue_depths": {k: q.qsize() for k, q in self._queues.items()},
            "dropped_frames": dict(self.dropped), "upload_failures": dict(self.upload_failures),
            "last_error": self.last_error,
            "detection_settings_version": self.activity.settings.version,
            "activity_rules": [type(rule).__name__ for rule in self.activity.rules],
            "activity_rule_failures": dict(self.activity.failures),
            "clips": self.clips.health() if self.clips is not None else None,
            "cloud": (self.inference_uplink.health()
                      if hasattr(self.inference_uplink, "health") else None),
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Router is closed; create a new instance")
            if self._running:
                return
            self._running = self._accepting = True
            if self.clips is not None:
                self.clips.start()
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
        observed = time.time()*1000-ts_ms
        self._capture_offset_ms = observed if self._capture_offset_ms is None else min(self._capture_offset_ms,observed)
        work = FrameWork(image, ts_ms, int(ts_ms+self._capture_offset_ms))
        self._last_capture = self._clock()
        if self.clips is not None:
            try:
                self.clips.add_frame(image, ts_ms)
            except Exception as exc:  # noqa: BLE001 -- clips never block capture
                self.last_error = f"clips: {type(exc).__name__}"
        if (self.inference_uplink is not None and self._sampler.should_keep(ts_ms)
                and self._cloud_sampler.should_keep(ts_ms)):
            self._enqueue("cloud", work)
        # Live view in every mode: family members watch whatever the camera sees.
        if self.live_view_enabled and self._live_sampler.should_keep(ts_ms):
            self._enqueue("live", work)

    async def _worker(self, name: str) -> None:
        queue = self._queues[name]
        while True:
            work = await queue.get()
            try:
                if work is None:
                    return
                await self._upload(name, work)
            except Exception as exc:
                # Boundary around third-party transport implementations.
                self.last_error = f"{name}: {type(exc).__name__}"
                self.upload_failures[name] += 1
                if name == "cloud":
                    self._cloud_error = type(exc).__name__
            finally:
                queue.task_done()

    async def _upload(self, name: str, work: FrameWork) -> None:
        rate = self._live_rate() if name == "live" else self.cloud_upload_fps
        now = self._clock()
        if name in self._last_attempt and now - self._last_attempt[name] + 1e-9 < 1.0 / rate:
            self.dropped[name] += 1
            return
        jpeg = await asyncio.to_thread(self.frame_uplink.encode, work.image)
        if jpeg is None:
            self.upload_failures[name] += 1
            if name == "cloud":
                self._cloud_error = "jpeg_encode_failed"
            return
        # Measure from the actual send, after potentially slow JPEG encoding.
        # Failures and mode changes do not reset this attempt budget.
        self._last_attempt[name] = self._clock()
        if name == "live":
            # A round trip to the server is longer than a video frame
            # interval, so several live frames are in flight at once.
            await self._live_slots.acquire()
            task = asyncio.create_task(self._push_live(jpeg, work.captured_at_ms))
            self._live_tasks.add(task)
            task.add_done_callback(self._live_tasks.discard)
            return
        else:
            request = self.inference_uplink.submit(jpeg, camera_id=self.camera_id,
                                                   ts_ms=work.ts_ms)
        if not await asyncio.wait_for(request, timeout=self.upload_timeout_s):
            self.upload_failures[name] += 1
            self._cloud_error = "inference_not_processed"
        else:
            self._last_processed = self._clock()
            self._last_processed_at = time.time()
            self._cloud_error = None

    async def _push_live(self, jpeg: bytes, captured_at_ms: int | None) -> None:
        try:
            pushed = await asyncio.wait_for(
                self.frame_uplink.push(jpeg, captured_at_ms=captured_at_ms),
                timeout=self.upload_timeout_s)
            if not pushed:
                self.upload_failures["live"] += 1
        except Exception as exc:  # noqa: BLE001 -- a lost frame is just dropped
            self.upload_failures["live"] += 1
            self.last_error = f"live: {type(exc).__name__}"
        finally:
            self._live_slots.release()

    def _live_rate(self) -> float:
        current = getattr(self.frame_uplink, "current_fps", None)
        return min(self.live_view_fps, current()) if callable(current) else self.live_view_fps

    def _discard_pending(self) -> None:
        for name, queue in self._queues.items():
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
                self.dropped[name] += 1

    async def wait_idle(self) -> None:
        for queue in self._queues.values():
            await queue.join()
        if self._live_tasks:
            await asyncio.gather(*list(self._live_tasks), return_exceptions=True)

    async def change_mode(self, mode: InferenceMode) -> None:
        """Record the requested mode; the effective mode stays CLOUD."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Router is closed")
            self.requested_mode = InferenceMode(mode)
            self.selection = select_inference_mode(mode, self.capabilities)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._accepting = False
            self._discard_pending()
            await self.wait_idle()
            if self._tasks:
                for queue in self._queues.values():
                    queue.put_nowait(None)
            await asyncio.gather(*self._tasks)
            self._running = False
            self._closed = True
            try:
                if self.clips is not None:
                    await self.clips.close()
            finally:
                try:
                    await self.frame_uplink.close()
                finally:
                    if self.inference_uplink is not None:
                        await self.inference_uplink.close()
