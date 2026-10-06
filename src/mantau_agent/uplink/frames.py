"""Push JPEG frames to the server for the app's live view.

Separate from `UplinkClient` on purpose. Events are precious -- they get a
sequence number, a signature over a canonical envelope, and a durable spool
so an outage can't lose a fall. Frames are the opposite: only the newest one
matters, so a failed push is dropped rather than retried, and nothing is
spooled. Retrying a frame would just show the viewer something that already
stopped being true.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

import cv2
import httpx

from ..camera.puller import CameraPuller


class FrameUplink:
    def __init__(
        self,
        server_url: str,
        agent_id: str,
        secret: str,
        camera_id: str,
        *,
        fps: float = 10.0,
        idle_fps: float = 1.0,
        live_hold_s: float = 5.0,
        jpeg_quality: int = 70,
        max_width: int = 640,
        max_bytes: int = 256 * 1024,
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self.camera_id = camera_id
        self.fps = fps
        self.idle_fps = min(idle_fps, fps)
        self._live_hold_s = live_hold_s
        self._live_until = float("-inf")
        self._clock = clock
        self._jpeg_quality = jpeg_quality
        self._max_width = max_width
        self._max_bytes = max_bytes
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None

    def current_fps(self) -> float:
        """Video rate while the server reported a viewer recently, else idle."""
        return self.fps if self._clock() < self._live_until else self.idle_fps

    def encode(self, image) -> bytes | None:
        # Detection doesn't need 1080p and neither does a phone screen -- the
        # brief already picks the camera's sub-stream for the same reason.
        height, width = image.shape[:2]
        if width > self._max_width:
            scale = self._max_width / width
            image = cv2.resize(image, (self._max_width, int(height * scale)))
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])
        if not ok:
            return None
        jpeg = buf.tobytes()
        return jpeg if len(jpeg) <= self._max_bytes else None

    def _signature(self, jpeg: bytes) -> str:
        return hmac.new(
            self._secret.encode("utf-8"),
            self.camera_id.encode("utf-8") + b"." + jpeg,
            hashlib.sha256,
        ).hexdigest()

    async def push(self, jpeg: bytes, captured_at_ms: int | None = None) -> bool:
        if len(jpeg) > self._max_bytes:
            return False
        headers = {
            "Content-Type": "image/jpeg",
            "X-Mantau-Agent": self.agent_id,
            "X-Mantau-Signature": self._signature(jpeg),
        }
        if captured_at_ms is not None:
            # Lets the server drop a frame that overtook a newer one.
            headers["X-Mantau-Captured-At"] = str(captured_at_ms)
        try:
            resp = await self._client.post(
                f"{self.server_url}/cameras/{self.camera_id}/frame",
                content=jpeg, headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return False
        try:
            viewers = int(resp.headers.get("X-Mantau-Live-Viewers", "0"))
        except ValueError:
            viewers = 0
        if viewers > 0:
            self._live_until = self._clock() + self._live_hold_s
        return True

    async def run(self, puller: CameraPuller, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            latest = puller.latest_frame()
            if latest is not None:
                jpeg = self.encode(latest[0])
                if jpeg is not None:
                    await self.push(jpeg)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0 / self.current_fps())
            except TimeoutError:
                continue

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
