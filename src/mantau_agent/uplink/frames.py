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
        fps: float = 4.0,
        jpeg_quality: int = 70,
        max_width: int = 640,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self.camera_id = camera_id
        self._interval_s = 1.0 / fps if fps > 0 else 0.0
        self._jpeg_quality = jpeg_quality
        self._max_width = max_width
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None

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
        return buf.tobytes()

    def _signature(self, jpeg: bytes) -> str:
        return hmac.new(
            self._secret.encode("utf-8"),
            self.camera_id.encode("utf-8") + b"." + jpeg,
            hashlib.sha256,
        ).hexdigest()

    async def push(self, jpeg: bytes) -> bool:
        try:
            resp = await self._client.post(
                f"{self.server_url}/cameras/{self.camera_id}/frame",
                content=jpeg,
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Mantau-Agent": self.agent_id,
                    "X-Mantau-Signature": self._signature(jpeg),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def run(self, puller: CameraPuller, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            latest = puller.latest_frame()
            if latest is not None:
                jpeg = self.encode(latest[0])
                if jpeg is not None:
                    await self.push(jpeg)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
