"""Lifecycle of the existing capture, routing and heartbeat components."""

from __future__ import annotations

import asyncio
import logging

from mantau_core.resilience import BackoffPolicy, Supervisor

from .health.heartbeat import HeartbeatLoop

log = logging.getLogger(__name__)


class MonitoringPipeline:
    def __init__(self, *, puller, router, uplink, spool, tunnel,
                 agent_id: str, camera_id: str, poll_interval_s: float = 0.02,
                 heartbeat_interval_s: float = 30.0) -> None:
        self.puller, self.router = puller, router
        self.uplink, self.spool, self.tunnel = uplink, spool, tunnel
        self.poll_interval_s = poll_interval_s
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._closed = False
        self._lock = asyncio.Lock()
        self.heartbeat = HeartbeatLoop(
            agent_id, camera_id, uplink, interval_s=heartbeat_interval_s,
            camera_reachable=lambda: puller.reachable,
            detector_alive=lambda: router.detector_alive,
            queue_depth=lambda: spool.depth() + sum(router.health()["queue_depths"].values()),
            local_status=router.health,
        )

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Pipeline is closed; create a new instance")
            if self._started:
                return
            try:
                await self.tunnel.up()
                self.puller.start()
                await self.router.start()
                self._tasks = [
                    asyncio.create_task(self._capture(), name="pipeline-capture"),
                    asyncio.create_task(Supervisor(
                        "heartbeat", lambda: self.heartbeat.run(stop_event=self._stop),
                        backoff=BackoffPolicy(base=1.0, cap=15.0),
                    ).run_forever(stop_event=self._stop), name="pipeline-heartbeat"),
                ]
                self._started = True
            except BaseException:
                await self._shutdown()
                raise

    async def _capture(self) -> None:
        while not self._stop.is_set():
            frame = self.puller.latest_frame()
            if frame is not None:
                self.router.submit(*frame)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def change_mode(self, mode) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Pipeline is closed")
            await self.router.change_mode(mode)
            log.info("Inference routing: %s", self.router.health())

    def health(self) -> dict:
        return {"capabilities": self.router.capabilities.model_dump(mode="json"),
                "routing": self.router.health(), "camera_reachable": self.puller.reachable,
                "spool_depth": self.spool.depth()}

    async def shutdown(self) -> None:
        async with self._lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._closed:
            return
        self._stop.set()
        try:
            await asyncio.gather(*self._tasks)
        finally:
            try:
                await asyncio.to_thread(self.puller.stop)
            finally:
                try:
                    await self.router.shutdown()
                finally:
                    try:
                        await self.uplink.close()
                    finally:
                        try:
                            self.spool.close()
                        finally:
                            self._closed = True
                            self._started = False
                            await self.tunnel.down()
