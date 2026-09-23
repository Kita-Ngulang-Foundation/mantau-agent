"""Lifecycle of the existing capture, routing and heartbeat components."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from mantau_core.resilience import BackoffPolicy, Supervisor

from .health.heartbeat import HeartbeatLoop
from .health.status import StatusStore

log = logging.getLogger(__name__)


class MonitoringPipeline:
    def __init__(self, *, puller, router, uplink, spool, tunnel,
                 agent_id: str, camera_id: str, poll_interval_s: float = 0.02,
                 heartbeat_interval_s: float = 30.0,
                 spool_backoff: BackoffPolicy | None = None,
                 status_store: StatusStore | None = None,
                 status_interval_s: float = 5.0, control_worker=None) -> None:
        self.puller, self.router = puller, router
        self.uplink, self.spool, self.tunnel = uplink, spool, tunnel
        self.poll_interval_s = poll_interval_s
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._started_monotonic: float | None = None
        self._started_at: datetime | None = None
        self._spool_backoff = spool_backoff or BackoffPolicy(base=0.5, cap=15.0)
        self._status_store = status_store
        self._status_interval_s = status_interval_s
        self.control_worker = control_worker
        self.restart_requested = asyncio.Event()
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
                self._started_monotonic = time.monotonic()
                self._started_at = datetime.now(timezone.utc)
                self._tasks = [
                    asyncio.create_task(self._capture(), name="pipeline-capture"),
                    asyncio.create_task(Supervisor(
                        "heartbeat", lambda: self.heartbeat.run(stop_event=self._stop),
                        backoff=BackoffPolicy(base=1.0, cap=15.0),
                    ).run_forever(stop_event=self._stop), name="pipeline-heartbeat"),
                    asyncio.create_task(self._drain_spool(), name="pipeline-spool-drain"),
                ]
                if self._status_store is not None:
                    self._tasks.append(asyncio.create_task(
                        self._write_status_loop(), name="pipeline-status"))
                if self.control_worker is not None:
                    self._tasks.append(asyncio.create_task(
                        self.control_worker.run(self._stop), name="pipeline-control"))
                self._started = True
            except BaseException:
                await self._shutdown()
                raise

    async def _capture(self) -> None:
        was_reachable = False
        while not self._stop.is_set():
            reachable = bool(getattr(self.puller, "reachable", True))
            if was_reachable and not reachable:
                # Activity timers pause across the outage instead of counting it.
                self.router.activity.camera_lost()
            was_reachable = reachable
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
            self._write_status()

    async def _drain_spool(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self.spool.depth() == 0:
                attempt = 0
                await self._wait_for_retry_or_stop()
                continue
            sent = await self.uplink.drain_spool()
            if self.spool.depth() == 0 or sent:
                attempt = 0
                continue
            delay = self._spool_backoff.delay_for(attempt)
            attempt = min(attempt + 1, 63)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _wait_for_retry_or_stop(self) -> None:
        retry = asyncio.create_task(self.uplink.wait_for_retry(timeout_s=3600.0))
        stopped = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait((retry, stopped), return_when=asyncio.FIRST_COMPLETED)
        finally:
            retry.cancel()
            stopped.cancel()
            await asyncio.gather(retry, stopped, return_exceptions=True)

    async def _write_status_loop(self) -> None:
        while not self._stop.is_set():
            self._write_status()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._status_interval_s)
            except asyncio.TimeoutError:
                pass

    def _write_status(self) -> None:
        if self._status_store is not None:
            try:
                self._status_store.write(self.health())
            except OSError as exc:
                # A read-only/full filesystem should be visible in logs, but
                # must not terminate monitoring or prevent clean shutdown.
                log.warning("Could not write local status: %s", type(exc).__name__)

    def health(self) -> dict:
        uptime = (time.monotonic() - self._started_monotonic
                  if self._started_monotonic is not None else 0.0)
        last_frame = getattr(self.puller, "last_frame_at", None)
        last_contact = self.uplink.last_successful_contact
        return {
            "running": self._started and not self._closed,
            "pid": os.getpid(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "version": self.router.capabilities.software_version,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "uptime_seconds": max(0.0, uptime),
            "camera": {
                "connected": self.puller.reachable,
                "last_frame_at": last_frame.isoformat() if last_frame else None,
                "last_error": getattr(self.puller, "last_error", None),
                "restarts": getattr(self.puller, "restarts", 0),
            },
            "effective_inference_mode": self.router.mode.value,
            "detector": {"alive": self.router.detector_alive,
                         "backend": self.router.capabilities.detector_backend},
            "uplink": {
                "connected": self.uplink.server_reachable,
                "last_successful_server_contact": last_contact.isoformat() if last_contact else None,
                "last_error": self.uplink.last_error,
            },
            "spool_depth": self.spool.depth(),
            "routing": self.router.health(),
            "capabilities": self.router.capabilities.model_dump(mode="json"),
        }

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
                    self._closed = True
                    self._started = False
                    try:
                        self._write_status()
                    finally:
                        try:
                            await self.uplink.close()
                        finally:
                            try:
                                self.spool.close()
                            finally:
                                try:
                                    if self.control_worker is not None:
                                        await self.control_worker.close()
                                finally:
                                    await self.tunnel.down()
