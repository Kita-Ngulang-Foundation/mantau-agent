"""Periodic liveness ping -- reuses the same `UplinkClient` (and therefore
the same spool-on-failure behavior) as fall events. A heartbeat that can't
reach the server right now just spools like anything else; it is not
special-cased.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from mantau_core.contracts import Heartbeat

from ..uplink.client import UplinkClient


class HeartbeatLoop:
    """Reads current status via three injected callables at send time, so
    this loop doesn't need to know how "is the camera reachable" or "how
    deep is the spool" are tracked elsewhere (`CameraPuller.reachable`,
    `EnvelopeSpool.depth`). The pipeline injects the router's detector status;
    defaults remain compatible with existing standalone callers. Rich local
    status is logged separately because core's heartbeat has no such fields."""

    def __init__(
        self,
        agent_id: str,
        camera_id: str | None,
        uplink: UplinkClient,
        *,
        interval_s: float = 30.0,
        camera_reachable: Callable[[], bool] = lambda: True,
        detector_alive: Callable[[], bool] = lambda: True,
        queue_depth: Callable[[], int] = lambda: 0,
        local_status: Callable[[], dict] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.camera_id = camera_id
        self.uplink = uplink
        self.interval_s = interval_s
        self._camera_reachable = camera_reachable
        self._detector_alive = detector_alive
        self._queue_depth = queue_depth
        self._local_status = local_status

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if self._local_status is not None:
                logging.getLogger(__name__).info("Pipeline health: %s", self._local_status())
            heartbeat = Heartbeat(
                agent_id=self.agent_id,
                camera_id=self.camera_id,
                camera_reachable=self._camera_reachable(),
                detector_alive=self._detector_alive(),
                queue_depth=self._queue_depth(),
            )
            await self.uplink.send_heartbeat(heartbeat)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass  # normal case -- interval elapsed, loop again
