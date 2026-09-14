"""describe() against real local sockets, including a genuinely chunked
DESCRIBE response -- proving the Content-Length read loop actually spans
multiple `reader.read()` calls, not just a single lucky one.
"""

from __future__ import annotations

import asyncio

import pytest
from mantau_core.contracts import CameraRef, StreamProfile

from mantau_agent.camera.rtsp_client import describe

SDP_BODY = (
    "v=0\r\no=- 0 0 IN IP4 192.168.1.42\r\ns=Media Server\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\nb=AS:512\r\na=control:trackID=1\r\n"
)


def _camera_for(server: asyncio.base_events.Server) -> CameraRef:
    host, port = server.sockets[0].getsockname()[:2]
    return CameraRef(camera_id="cam-1", name="Test Cam", host="127.0.0.1", port=port,
                      paths={StreamProfile.MAIN: "/stream1"})


async def test_describe_returns_the_sdp_body():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")  # OPTIONS
        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n")
        await writer.drain()

        await reader.readuntil(b"\r\n\r\n")  # DESCRIBE
        body_bytes = SDP_BODY.encode("utf-8")
        writer.write(
            f"RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Type: application/sdp\r\n"
            f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("utf-8") + body_bytes
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        body = await describe(_camera_for(server), timeout_s=2.0)
    finally:
        server.close()
    assert body == SDP_BODY


async def test_describe_reassembles_a_body_delivered_in_two_writes():
    """The real regression this guards against: a single reader.read() call
    is not guaranteed to return the whole response, especially a body over
    a TCP segment boundary -- writing it in two pieces with a delay between
    them is what actually forces the read loop to run more than once."""

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
        await writer.drain()

        await reader.readuntil(b"\r\n\r\n")
        body_bytes = SDP_BODY.encode("utf-8")
        header = (
            f"RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Length: {len(body_bytes)}\r\n\r\n"
        ).encode("utf-8")
        split = len(body_bytes) // 2

        writer.write(header + body_bytes[:split])
        await writer.drain()
        await asyncio.sleep(0.05)  # force two distinct TCP segments
        writer.write(body_bytes[split:])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        body = await describe(_camera_for(server), timeout_s=2.0)
    finally:
        server.close()
    assert body == SDP_BODY


async def test_describe_raises_on_a_non_200_response():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
        await writer.drain()

        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"RTSP/1.0 404 Not Found\r\nCSeq: 2\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        with pytest.raises(ConnectionError, match="DESCRIBE failed"):
            await describe(_camera_for(server), timeout_s=2.0)
    finally:
        server.close()
