"""RTSP OPTIONS -> DESCRIBE, far enough to fetch and log the camera's SDP.

Actually opening and reading the stream (`SETUP` + `PLAY`, RTP packet
reassembly, H.264 depacketization) is delegated to OpenCV's own RTSP client
inside `camera/puller.py` via `cv2.VideoCapture` -- hand-rolling a full
RTP/RTSP media pipeline is real, substantial work well beyond this repo's
scope, and OpenCV's FFmpeg backend already does it correctly. This module's
DESCRIBE-only reach is what's actually needed: enough of the handshake to
read the SDP body once at startup and pick a stream by bandwidth (see
`profile.py`) -- a diagnostic/selection step, not the frame-reading path.

Raises on failure (unlike `discovery/probe.py`, which never raises) --
this is a one-shot startup step where a clear exception is more useful than
a classified "reason it failed" result.
"""

from __future__ import annotations

import asyncio
import re

from mantau_core.contracts import CameraRef, StreamProfile

_STATUS_LINE = re.compile(r"^RTSP/\d\.\d\s+(\d{3})\b")


def _parse_status(header_text: str) -> int | None:
    first_line = header_text.splitlines()[0] if header_text.splitlines() else ""
    match = _STATUS_LINE.match(first_line)
    return int(match.group(1)) if match else None


async def _read_rtsp_response(reader: asyncio.StreamReader, timeout_s: float) -> str:
    """Read a full response: headers up to the blank line, then exactly
    `Content-Length` bytes of body -- a real SDP body is often well over
    1KB and a single `reader.read()` call is not guaranteed to return it
    all at once, especially over multiple TCP segments."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
        if not chunk:
            break
        buf += chunk
    header_bytes, _, rest = buf.partition(b"\r\n\r\n")
    header_text = header_bytes.decode("utf-8", errors="replace")

    content_length = 0
    for line in header_text.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                content_length = 0
            break

    body = rest
    while len(body) < content_length:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
        if not chunk:
            break
        body += chunk

    return header_text + "\r\n\r\n" + body.decode("utf-8", errors="replace")


async def describe(
    camera: CameraRef, *, profile: StreamProfile = StreamProfile.MAIN, timeout_s: float = 5.0
) -> str:
    """OPTIONS then DESCRIBE against `camera`. Returns the SDP body as text.

    Raises `ConnectionError` on a non-200 DESCRIBE response or a connection
    failure -- callers that want a never-raises reachability check should
    use `discovery/probe.py` instead; this one exists for startup
    diagnostics where a clear failure is more useful than a soft result.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(camera.host, camera.port), timeout=timeout_s
    )
    try:
        url = camera.stream_url(profile)
        writer.write(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode("utf-8"))
        await writer.drain()
        await _read_rtsp_response(reader, timeout_s)  # discard -- just clears the handshake

        writer.write(
            f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n".encode("utf-8")
        )
        await writer.drain()
        response = await _read_rtsp_response(reader, timeout_s)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout_s)
        except (OSError, asyncio.TimeoutError):
            pass

    header_text, _, body = response.partition("\r\n\r\n")
    status = _parse_status(header_text)
    if status != 200:
        first_line = header_text.splitlines()[0] if header_text else "no response"
        raise ConnectionError(f"DESCRIBE failed for camera {camera.camera_id!r}: {first_line}")
    return body
