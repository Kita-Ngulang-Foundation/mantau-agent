"""probe_camera_on_lan against real local sockets -- same approach as
mantau-backend-rtsp's reachability probe tests (see that module for why
each fake-server handler closes its own writer and why servers are closed
without awaiting `wait_closed()`).
"""

from __future__ import annotations

import asyncio

from mantau_core.contracts import AttemptedFrom, CameraRef, ReachabilityErrorKind, StreamProfile

from mantau_agent.discovery.probe import probe_camera_on_lan


def _camera_for(server: asyncio.base_events.Server) -> CameraRef:
    host, port = server.sockets[0].getsockname()[:2]
    return CameraRef(camera_id="cam-1", name="Test Cam", host="127.0.0.1", port=port,
                      paths={StreamProfile.SUB: "/stream2"})


async def test_reachable_on_a_valid_rtsp_response():
    async def handler(reader, writer):
        await reader.read(1024)
        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        result = await probe_camera_on_lan(_camera_for(server), timeout_s=2.0)
    finally:
        server.close()
    assert result is None


async def test_result_is_always_attempted_from_lan():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    server.close()
    await server.wait_closed()

    camera = CameraRef(camera_id="cam-1", name="Test Cam", host="127.0.0.1", port=port,
                        paths={StreamProfile.SUB: "/stream2"})
    result = await probe_camera_on_lan(camera, timeout_s=1.0)
    assert result is not None
    assert result.attempted_from is AttemptedFrom.LAN


async def test_auth_failed_on_401():
    async def handler(reader, writer):
        await reader.read(1024)
        writer.write(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        result = await probe_camera_on_lan(_camera_for(server), timeout_s=2.0)
    finally:
        server.close()
    assert result is not None
    assert result.kind is ReachabilityErrorKind.AUTH_FAILED


async def test_timeout_when_the_camera_never_responds():
    async def handler(reader, writer):
        await reader.read(1024)
        await asyncio.sleep(2)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        result = await probe_camera_on_lan(_camera_for(server), timeout_s=0.3)
    finally:
        server.close()
    assert result is not None
    assert result.kind is ReachabilityErrorKind.TIMEOUT


async def test_dns_failure_on_an_unresolvable_hostname():
    # .invalid is reserved by RFC 2606 specifically to never resolve. Most
    # resolvers report that fast (DNS_FAILURE), but a slow/retrying resolver
    # can exceed the probe's own timeout first (TIMEOUT) -- either is a
    # correct "not reachable" outcome; the resolver's own timing isn't what's
    # under test here (see mantau-backend-rtsp's own version of this test).
    camera = CameraRef(camera_id="cam-1", name="Test Cam",
                        host="definitely-not-a-real-camera.invalid", port=554,
                        paths={StreamProfile.SUB: "/stream2"})
    result = await probe_camera_on_lan(camera, timeout_s=2.0)
    assert result is not None
    assert result.kind in (ReachabilityErrorKind.DNS_FAILURE, ReachabilityErrorKind.TIMEOUT)
