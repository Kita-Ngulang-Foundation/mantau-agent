"""Server inference: upload sampled frames, the server runs the fall detector.

`InferenceUplink` is what the router depends on. `HttpInferenceUplink` is
the real adapter for `POST /agents/{id}/inference`; the wire contract
(authentication, frame timestamps, event correlation, idempotency,
confirmation results and retention) is `mantau_core.contracts.inference`.

Frames stay disposable: a failed upload is retried at most `retries` times
with the same frame id (the server answers a retry from its idempotency
store instead of running the detector twice), only while the frame is still
fresh, and then dropped. Nothing is spooled. The /cameras/{id}/frame
endpoint remains live-view storage only and is never used for inference.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from collections.abc import Callable
from typing import Protocol

import httpx
from mantau_core.contracts import FallEvent, InferenceCapability, InferenceResult
from mantau_core.contracts import inference as contract

log = logging.getLogger(__name__)


class InferenceUplink(Protocol):
    async def submit(self, jpeg: bytes, *, camera_id: str, ts_ms: int,
                     event_ids: tuple[str, ...] = ()) -> bool:
        """Accept a sampled frame; event_ids identifies HYBRID confirmation.

        True means the server processed (or had already processed) the frame.
        False or an exception is a failed upload; frames are disposable.
        """
        ...

    async def close(self) -> None: ...


async def discover_capability(server_url: str, *, client: httpx.AsyncClient | None = None,
                              timeout_s: float = 5.0) -> InferenceCapability | None:
    """The server's `GET /inference/capability`, or None if it cannot be read
    (older server, unreachable): then CLOUD simply is not available."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.get(f"{server_url.rstrip('/')}/inference/capability")
        if resp.status_code != 200:
            return None
        return InferenceCapability.model_validate(resp.json())
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owned:
            await client.aclose()


class HttpInferenceUplink:
    _RETRYABLE = {500, 502, 503, 504}

    def __init__(self, server_url: str, agent_id: str, secret: str,
                 capability: InferenceCapability, *, client: httpx.AsyncClient | None = None,
                 retries: int = 1, retry_base_s: float = 0.1,
                 on_events: Callable[[list[FallEvent]], None] | None = None,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self.capability = capability
        self.session_id = uuid.uuid4().hex
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._retries = max(0, retries)
        self._retry_base_s = retry_base_s
        self.on_events = on_events
        self._wall = wall_clock
        # Smallest (wall ms - stream ms) seen: the stream clock's offset from
        # wall-clock time plus the least queueing delay, so each frame's capture
        # time is its own stream time plus this, not "whenever it was sent".
        self._offset_ms: float | None = None
        self.stats = {"accepted": 0, "failed": 0, "retried": 0, "events": 0,
                      "confirmations": 0, "too_large": 0}
        self.last_result: InferenceResult | None = None
        self.last_error: str | None = None

    def captured_at_ms(self, ts_ms: int) -> int:
        observed = self._wall() * 1000 - ts_ms
        self._offset_ms = observed if self._offset_ms is None else min(self._offset_ms, observed)
        return int(ts_ms + self._offset_ms)

    async def submit(self, jpeg: bytes, *, camera_id: str, ts_ms: int,
                     event_ids: tuple[str, ...] = ()) -> bool:
        if len(jpeg) > self.capability.max_frame_bytes:
            self.stats["too_large"] += 1
            return False
        event_ids = tuple(event_ids)[:contract.MAX_EVENT_IDS]
        frame_id = uuid.uuid4().hex
        captured_at_ms = self.captured_at_ms(ts_ms)
        fields = dict(agent_id=self.agent_id, camera_id=camera_id, session_id=self.session_id,
                      frame_id=frame_id, ts_ms=int(ts_ms), captured_at_ms=captured_at_ms,
                      event_ids=list(event_ids))
        headers = {
            "Content-Type": "image/jpeg",
            "X-Mantau-Agent": self.agent_id,
            "X-Mantau-Camera": camera_id,
            "X-Mantau-Session": self.session_id,
            "X-Mantau-Frame": frame_id,
            "X-Mantau-Frame-Ts": str(int(ts_ms)),
            "X-Mantau-Captured-At": str(captured_at_ms),
            "X-Mantau-Signature": contract.sign(self._secret, body=jpeg, **fields),
        }
        if event_ids:
            headers["X-Mantau-Event-Ids"] = ",".join(event_ids)
        url = f"{self.server_url}/agents/{self.agent_id}/inference"
        for attempt in range(self._retries + 1):
            if attempt:
                age_s = (self._wall() * 1000 - captured_at_ms) / 1000
                if age_s >= self.capability.max_frame_age_s:
                    break  # a stale frame is worth nothing; drop it
                self.stats["retried"] += 1
                await asyncio.sleep(self._retry_base_s * (2 ** (attempt - 1))
                                    * (0.5 + random.random() / 2))
            try:
                resp = await self._client.post(url, content=jpeg, headers=headers)
            except httpx.HTTPError as exc:
                self.last_error = type(exc).__name__
                continue
            if resp.status_code == 200:
                return self._accept(resp)
            # Status code only: server error bodies never carry frame content,
            # and nothing secret is in them, but keep logs uniform and small.
            self.last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in self._RETRYABLE:
                break
        self.stats["failed"] += 1
        return False

    def _accept(self, resp: httpx.Response) -> bool:
        try:
            result = InferenceResult.model_validate(resp.json())
        except ValueError:
            self.last_error = "invalid_response"
            self.stats["failed"] += 1
            return False
        self.last_result, self.last_error = result, None
        self.stats["accepted"] += 1
        self.stats["events"] += len(result.events)
        self.stats["confirmations"] += len(result.confirmations)
        # A duplicate here means an earlier attempt of this same upload reached
        # the server but its answer was lost, so these events are still new to us.
        if result.events and self.on_events is not None:
            try:
                self.on_events(list(result.events))
            except Exception as exc:  # noqa: BLE001 -- a clip hook must not fail the upload
                log.warning("server-event hook failed (%s)", type(exc).__name__)
        return result.processed

    def health(self) -> dict:
        return {"session_id": self.session_id, "max_fps": self.capability.max_fps,
                "last_error": self.last_error, **self.stats}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def cloud_rate(requested_fps: float, capability: InferenceCapability) -> float:
    """Upload rate actually used: what was asked for, capped by the server."""
    rate = min(requested_fps, capability.max_fps)
    return rate if math.isfinite(rate) and rate > 0 else capability.max_fps
