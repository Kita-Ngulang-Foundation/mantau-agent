"""Short review clips around events: a few seconds before and after.

Frames are kept as JPEG in a small ring buffer at a low rate. When an event
is sent, the recorder keeps the pre-event frames, collects the post-event
frames, encodes an MP4, writes it to a spool directory, and uploads it. A clip
that cannot be uploaded stays on disk and is retried; it never blocks the
event itself, which has already gone out through the durable envelope path.

Bathroom-duration events are never recorded: that zone is private by design.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import shutil
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from mantau_core.contracts import EventKind, FallEvent

log = logging.getLogger(__name__)

# Server answers that retrying can never fix.
_PERMANENT = {400, 401, 403, 404, 413, 415}


def encode_mp4(frames: list[bytes], fps: float) -> bytes:
    """H.264 MP4 (plays everywhere, including Android's player) via ffmpeg;
    falls back to OpenCV's MPEG-4 writer when ffmpeg is missing."""
    if not frames:
        raise ValueError("no frames")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
                 "-framerate", f"{fps:g}", "-c:v", "mjpeg", "-i", "-",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 "-movflags", "+faststart", str(out)],
                input=b"".join(frames), capture_output=True, timeout=60, check=False,
            )
            if result.returncode == 0 and out.exists():
                return out.read_bytes()
            log.warning("ffmpeg clip encoding failed (exit %s); using OpenCV", result.returncode)
    return _encode_with_opencv(frames, fps)


def _encode_with_opencv(frames: list[bytes], fps: float) -> bytes:
    import cv2
    import numpy as np

    images = [cv2.imdecode(np.frombuffer(f, dtype=np.uint8), cv2.IMREAD_COLOR) for f in frames]
    images = [image for image in images if image is not None]
    if not images:
        raise ValueError("no decodable frames")
    height, width = images[0].shape[:2]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        try:
            for image in images:
                if image.shape[:2] != (height, width):
                    image = cv2.resize(image, (width, height))
                writer.write(image)
        finally:
            writer.release()
        return out.read_bytes()


class ClipUploader:
    """Signed upload: HMAC-SHA256(secret, "<event_id>." + body)."""

    def __init__(self, server_url: str, agent_id: str, secret: str, *,
                 client: httpx.AsyncClient | None = None) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def upload(self, event_id: str, body: bytes) -> bool | None:
        """True uploaded, False retry later, None never retry."""
        signature = hmac.new(self._secret.encode(), event_id.encode() + b"." + body,
                             hashlib.sha256).hexdigest()
        try:
            response = await self._client.post(
                f"{self.server_url}/events/{event_id}/recording", content=body,
                headers={"Content-Type": "video/mp4", "X-Mantau-Agent": self.agent_id,
                         "X-Mantau-Signature": signature},
            )
        except httpx.HTTPError:
            return False
        if response.status_code == 204:
            return True
        return None if response.status_code in _PERMANENT else False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass
class _Capture:
    event_id: str
    frames: list[bytes]
    remaining: int


class ClipRecorder:
    def __init__(self, *, uploader: ClipUploader, encode_jpeg: Callable[[object], bytes | None],
                 spool_dir: str | Path, fps: float = 5.0, pre_s: float = 5.0,
                 post_s: float = 5.0, max_pending_files: int = 50,
                 retry_interval_s: float = 60.0) -> None:
        self.uploader = uploader
        self.encode_jpeg = encode_jpeg
        self.spool_dir = Path(spool_dir)
        self.fps = fps
        self._interval_ms = 1000.0 / fps
        self._ring: deque[bytes] = deque(maxlen=max(1, round(pre_s * fps)))
        self._post_frames = max(1, round(post_s * fps))
        self._captures: list[_Capture] = []
        self._last_ts: int | None = None
        self._tasks: set[asyncio.Task] = set()
        self._max_pending = max_pending_files
        self._retry_interval_s = retry_interval_s
        self._retry_task: asyncio.Task | None = None
        self.uploaded = 0
        self.failed = 0

    def add_frame(self, image, ts_ms: int) -> None:
        """Called for every captured frame; keeps only `fps` of them."""
        if self._last_ts is not None and ts_ms - self._last_ts < self._interval_ms:
            return
        jpeg = self.encode_jpeg(image)
        if jpeg is None:
            return
        self._last_ts = ts_ms
        self._ring.append(jpeg)
        for capture in list(self._captures):
            capture.frames.append(jpeg)
            capture.remaining -= 1
            if capture.remaining <= 0:
                self._captures.remove(capture)
                self._spawn(self._finish(capture))

    def on_event(self, event: FallEvent) -> None:
        if event.kind is EventKind.BATHROOM_DURATION:
            return
        self._captures.append(_Capture(event.event_id, list(self._ring), self._post_frames))

    def start(self) -> None:
        if self._retry_task is None:
            self._retry_task = asyncio.create_task(self._retry_loop(), name="clip-retry")

    def health(self) -> dict:
        return {"pending_captures": len(self._captures), "uploaded": self.uploaded,
                "failed": self.failed, "spooled": len(self._spooled())}

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish(self, capture: _Capture) -> None:
        try:
            body = await asyncio.to_thread(encode_mp4, capture.frames, self.fps)
        except Exception as exc:  # noqa: BLE001 -- a clip must never break detection
            self.failed += 1
            log.warning("clip for %s not encoded (%s)", capture.event_id, type(exc).__name__)
            return
        path = self._store(capture.event_id, body)
        await self._send(path)

    def _store(self, event_id: str, body: bytes) -> Path:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        spooled = self._spooled()
        for old in spooled[: max(0, len(spooled) - self._max_pending + 1)]:
            old.unlink(missing_ok=True)  # bounded disk: drop the oldest clips first
        path = self.spool_dir / f"{event_id}.mp4"
        partial = path.with_suffix(".part")
        partial.write_bytes(body)
        partial.replace(path)
        return path

    def _spooled(self) -> list[Path]:
        if not self.spool_dir.exists():
            return []
        return sorted(self.spool_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)

    async def _send(self, path: Path) -> None:
        result = await self.uploader.upload(path.stem, path.read_bytes())
        if result is True:
            self.uploaded += 1
            path.unlink(missing_ok=True)
        elif result is None:
            self.failed += 1
            path.unlink(missing_ok=True)

    async def retry_spooled(self) -> None:
        for path in self._spooled():
            await self._send(path)

    async def _retry_loop(self) -> None:
        while True:
            try:
                await self.retry_spooled()
            except Exception as exc:  # noqa: BLE001
                log.warning("clip retry failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._retry_interval_s)

    async def close(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            await asyncio.gather(self._retry_task, return_exceptions=True)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.uploader.close()
