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
import math
import os
import re
import time
import shutil
import subprocess
import tempfile
import threading
from functools import wraps
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from mantau_core.contracts import EventKind, FallEvent

log = logging.getLogger(__name__)

# Server answers that retrying can never fix.
# A clip can reach the server before its event; 404 must be retried.
_PERMANENT = {400, 401, 403, 413, 415}


def _spool_locked(operation):
    @wraps(operation)
    def locked(self, *args, **kwargs):
        with self._spool_lock:
            return operation(self, *args, **kwargs)
    return locked


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
        return await self._upload(event_id, body)

    async def upload_transfer(self, event_id: str, transfer_id: str, body: bytes) -> bool | None:
        if not re.fullmatch(r"[a-f0-9]{32}", transfer_id):
            raise ValueError("invalid transfer identifier")
        return await self._upload(event_id, body, transfer_id)

    async def _upload(self, event_id, body, transfer_id=None):
        signature = hmac.new(self._secret.encode(), event_id.encode() + b"." + body,
                             hashlib.sha256).hexdigest()
        try:
            response = await self._client.post(
                f"{self.server_url}/events/{event_id}/recording", content=body,
                params={"transfer_id": transfer_id} if transfer_id else None,
                headers={"Content-Type": "video/mp4", "X-Mantau-Agent": self.agent_id,
                         "X-Mantau-Signature": signature},
            )
        except httpx.HTTPError:
            return False
        if response.status_code in (200, 204):
            return True
        return None if response.status_code in _PERMANENT else False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def legacy_claims(self, ids: list[str]) -> list[dict]:
        if len(ids) > 5:
            raise ValueError("legacy batch too large")
        response = await self._client.post(
            f"{self.server_url}/agent-control/recordings/legacy-check", json={"event_ids": ids},
            headers={"X-Mantau-Agent-ID": self.agent_id, "X-Mantau-Agent-Secret": self._secret})
        response.raise_for_status()
        rows = response.json()["recordings"]
        if len(rows) > 5 or any(row["event_id"] not in ids for row in rows):
            raise ValueError("invalid legacy response")
        return rows


@dataclass
class _Capture:
    event_id: str
    frames: list[bytes]
    remaining: int
    generation: int = 0
    occurred_at: float | None = None


class ClipRecorder:
    def __init__(self, *, uploader: ClipUploader, encode_jpeg: Callable[[object], bytes | None],
                 spool_dir: str | Path, fps: float = 5.0, pre_s: float = 5.0,
                 post_s: float = 5.0, max_pending_files: int = 50,
                 retry_interval_s: float = 60.0, max_spool_bytes: int = 100 * 1024 * 1024,
                 max_spool_age_s: float = 24 * 60 * 60, max_pending_captures: int = 8,
                 max_encoding_tasks: int = 2, wall_clock: Callable[[], float] = time.time,
                 retain_local: bool = False, legacy_spool_dir: str | Path | None = None) -> None:
        if any(not math.isfinite(value) or value <= 0
               for value in (fps, pre_s, post_s, retry_interval_s, max_spool_age_s)):
            raise ValueError("Clip rates, durations and retry interval must be positive and finite")
        if min(max_pending_files, max_spool_bytes, max_pending_captures, max_encoding_tasks) < 1:
            raise ValueError("Clip storage and work limits must be positive")
        self.uploader = uploader
        self.encode_jpeg = encode_jpeg
        # The caller chooses the writable directory, including the systemd unit's
        # service-owned path. Relative development paths follow the working directory.
        self.spool_dir = Path(spool_dir).expanduser().resolve()
        self._spool_lock = threading.RLock()
        self._legacy_migrator = None
        self.fps = fps
        self._interval_ms = 1000.0 / fps
        self._ring: deque[bytes] = deque(maxlen=max(1, round(pre_s * fps)))
        self._post_frames = max(1, round(post_s * fps))
        self._captures: list[_Capture] = []
        self._last_ts: int | None = None
        self._tasks: set[asyncio.Task] = set()
        self._max_pending = max_pending_files
        self._max_spool_bytes = max_spool_bytes
        self._max_spool_age_s = max_spool_age_s
        self._max_captures = max_pending_captures
        self._max_encoding_tasks = max_encoding_tasks
        self._retry_interval_s = retry_interval_s
        self._retry_task: asyncio.Task | None = None
        self._wall = wall_clock
        self._generation = 0
        self.retain_local = retain_local
        self.enabled = True
        self._seen: dict[str, None] = {}
        self.uploaded = 0
        self.failed = 0
        self.expired = 0
        self.evicted_capacity = 0
        self.retry_failures = 0
        self.persistence_failures = 0
        self.last_error: str | None = None
        self._closed = False
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            # Incomplete writes from a crashed process cannot be playable clips.
            for partial in self.spool_dir.glob("*.part"):
                partial.unlink(missing_ok=True)
            self._maintain_spool()
        except OSError as exc:
            self._failure("spool_init", exc, persistence=True)
        if retain_local and legacy_spool_dir is not None:
            from .legacy_clips import LegacyClipMigrator
            self._legacy_migrator = LegacyClipMigrator(Path(legacy_spool_dir), self)

    def _failure(self, stage: str, exc: Exception, *, persistence: bool = False) -> None:
        self.failed += 1
        self.persistence_failures += int(persistence)
        self.last_error = f"{stage}: {type(exc).__name__}"
        log.warning("clip operation failed (%s)", self.last_error)

    def clear_capture(self) -> None:
        """Forget the previous camera's ring and unfinished work; keep durable clips.

        Generation checks also discard an encoding result that completes after
        camera removal. Already stored historical clips retain their retry lifecycle.
        """
        self._generation += 1
        self._ring.clear()
        self._captures.clear()
        self._last_ts = None
        self._seen.clear()

    def add_frame(self, image, ts_ms: int) -> None:
        if not self.enabled or self._closed or (self._last_ts is not None and ts_ms - self._last_ts < self._interval_ms):
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
        if not self.enabled or self._closed or event.kind is EventKind.BATHROOM_DURATION or event.event_id in self._seen:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", event.event_id):
            self._failure("event_id", ValueError("invalid clip identifier"))
            return
        self._seen[event.event_id] = None
        while len(self._seen) > 512:
            self._seen.pop(next(iter(self._seen)))
        if len(self._captures) >= self._max_captures:
            self._failure("capture_capacity", RuntimeError("capture queue full"))
            return
        remaining = self._post_frames
        if self.retain_local:
            remaining += self._ring.maxlen - len(self._ring)
        self._captures.append(_Capture(event.event_id, list(self._ring), remaining, self._generation,
                                       event.occurred_at.timestamp()))

    def set_enabled(self, value: bool) -> None:
        if value != self.enabled:
            self.enabled = value
            self.clear_capture()

    @_spool_locked
    def inventory(self) -> list[dict]:
        return [{"event_id": path.stem, "size_bytes": path.stat().st_size,
                 "captured_at_ms": max(0, round(path.stat().st_mtime * 1000))}
                for path in reversed(self._maintain_spool())
                if 0 < path.stat().st_size <= 20 * 1024 * 1024][:5]

    async def transfer(self, event_id: str, transfer_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", event_id):
            raise ValueError("invalid clip identifier")
        with self._spool_lock:
            path = self.spool_dir / f"{event_id}.mp4"
            self._owned(path)
            if not path.exists() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
                return False
            body = path.read_bytes()
        return await self.uploader.upload_transfer(event_id, transfer_id, body) is True

    @_spool_locked
    def import_legacy(self, source: Path, occurred_at: float, is_current) -> str:
        if (source.is_symlink() or source.resolve() != source.absolute()
                or not 12 <= source.stat().st_size <= 20 * 1024 * 1024):
            raise ValueError("unowned legacy clip")
        body = source.read_bytes()
        if body[4:8] != b"ftyp" or not is_current() or self._closed:
            raise ValueError("legacy clip unavailable")
        target = self.spool_dir / source.name
        self._owned(target)
        if target.exists() and target.read_bytes() != body:
            raise ValueError("existing history differs")
        self._store(source.stem, body, occurred_at)
        if (target.exists() and target.read_bytes() != body) or source.read_bytes() != body or not is_current():
            raise ValueError("legacy copy verification failed")
        return hashlib.sha256(body).hexdigest()

    def _owned(self, path: Path) -> None:
        if self.spool_dir.resolve() != self.spool_dir or path.is_symlink() or path.resolve().parent != self.spool_dir:
            raise OSError("clip storage is not owned")

    def start(self) -> None:
        if not self._closed and self._retry_task is None:
            self._retry_task = asyncio.create_task(self._retry_loop(), name="clip-retry")

    @_spool_locked
    def health(self) -> dict:
        try:
            files = self._spooled()
            count, size = len(files), sum(path.stat().st_size for path in files)
        except OSError as exc:
            self._failure("spool_scan", exc, persistence=True)
            count, size = None, None
        return {"pending_captures": len(self._captures), "uploaded": self.uploaded,
                "failed": self.failed, "spooled": count, "spooled_bytes": size,
                "max_spool_bytes": self._max_spool_bytes, "expired": self.expired,
                "evicted_capacity": self.evicted_capacity, "retry_failures": self.retry_failures,
                "persistence_failures": self.persistence_failures, "last_error": self.last_error}

    def _spawn(self, coroutine) -> None:
        if len(self._tasks) >= self._max_encoding_tasks:
            coroutine.close()
            self._failure("encoding_capacity", RuntimeError("encoding queue full"))
            return
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish(self, capture: _Capture) -> None:
        if capture.generation != self._generation:
            return
        try:
            body = await asyncio.to_thread(encode_mp4, capture.frames, self.fps)
        except Exception as exc:  # encoding must never break capture
            self._failure("encoding", exc)
            return
        if capture.generation != self._generation:
            return
        try:
            path = self._store(capture.event_id, body, capture.occurred_at)
        except (OSError, ValueError) as exc:
            self._failure("persistence", exc, persistence=True)
            return
        if not self.retain_local:
            await self._send(path)

    @_spool_locked
    def _store(self, event_id: str, body: bytes, occurred_at: float | None = None) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}", event_id):
            raise ValueError("invalid clip identifier")
        if not body or len(body) > self._max_spool_bytes:
            raise ValueError("Encoded clip is empty or exceeds the spool byte limit")
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.spool_dir / f"{event_id}.mp4"
        self._owned(path)
        if path.exists():
            return path  # Keep the durable original if a duplicate event is replayed.
        spooled = [item for item in self._maintain_spool() if item != path]
        size = sum(item.stat().st_size for item in spooled)
        if self.retain_local and size + len(body) > self._max_spool_bytes:
            raise ValueError("clip spool byte limit")
        while not self.retain_local and spooled and (len(spooled) >= self._max_pending or size + len(body) > self._max_spool_bytes):
            oldest = spooled.pop(0)
            size -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            self.evicted_capacity += 1
        partial = path.with_suffix(".part")
        self._owned(partial)
        try:
            with partial.open("wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            partial.replace(path)
            if self.retain_local and occurred_at is not None:
                os.utime(path, (occurred_at, occurred_at))
            if os.name != "nt":
                directory_fd = os.open(self.spool_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            partial.unlink(missing_ok=True)
        self.last_error = None
        if self.retain_local:
            self._maintain_spool()
        return path

    @_spool_locked
    def _spooled(self) -> list[Path]:
        if not self.spool_dir.exists():
            return []
        files = list(self.spool_dir.glob("*.mp4"))
        for path in files:
            self._owned(path)
        return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))

    @_spool_locked
    def _maintain_spool(self) -> list[Path]:
        files = self._spooled()
        cutoff = self._wall() - self._max_spool_age_s
        retained = []
        for path in files:
            if not self.retain_local and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                self.expired += 1
            else:
                retained.append(path)
        size = sum(path.stat().st_size for path in retained)
        while retained and (len(retained) > self._max_pending or size > self._max_spool_bytes):
            path = retained.pop(0)
            size -= path.stat().st_size
            path.unlink(missing_ok=True)
            self.evicted_capacity += 1
        return retained

    async def _send(self, path: Path) -> None:
        try:
            result = await self.uploader.upload(path.stem, path.read_bytes())
            if result is True:
                self.uploaded += 1
                path.unlink(missing_ok=True)
                self.last_error = None
            elif result is None:
                self.failed += 1
                path.unlink(missing_ok=True)
            else:
                self.retry_failures += 1
        except OSError as exc:
            self._failure("spool_read", exc, persistence=True)
        except Exception as exc:
            self._failure("upload", exc)

    async def retry_spooled(self) -> None:
        try:
            paths = self._maintain_spool()
        except OSError as exc:
            self._failure("spool_maintenance", exc, persistence=True)
            return
        for path in ([] if self.retain_local else paths):
            await self._send(path)
        if self._legacy_migrator is not None:
            try:
                await self._legacy_migrator.step()
            except Exception as exc:
                self._failure("legacy_migration_pending", exc)

    async def _retry_loop(self) -> None:
        while True:
            await self.retry_spooled()
            await asyncio.sleep(self._retry_interval_s)

    async def close(self) -> None:
        self._closed = True
        if self._retry_task is not None:
            self._retry_task.cancel()
            await asyncio.gather(self._retry_task, return_exceptions=True)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.uploader.close()
