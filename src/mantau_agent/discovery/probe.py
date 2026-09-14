"""Confirm a discovered (or manually configured) camera actually speaks RTSP,
from the LAN -- the agent's own version of mantau-backend-rtsp's
`reachability/probe.py`, same two-stage TCP-connect-then-RTSP-OPTIONS
approach, deliberately duplicated rather than shared (see that repo's
module for the reasoning this pairs with).

The one thing genuinely different here: no CGNAT reclassification.
CGNAT is an internet-vantage-point concept (a shared carrier address seen
from OUTSIDE the customer's network) -- meaningless when the agent is
probing a camera on its own LAN. `AttemptedFrom.LAN` is always what gets
recorded.
"""

from __future__ import annotations

import asyncio
import re
import socket

from mantau_core.contracts import (
    AttemptedFrom,
    CameraRef,
    ReachabilityError,
    ReachabilityErrorKind,
    StreamProfile,
)


_RTSP_STATUS_LINE = re.compile(r"^RTSP/\d\.\d\s+(\d{3})\b")


def _parse_rtsp_status(response_text: str) -> int | None:
    if not response_text:
        return None
    first_line = response_text.splitlines()[0] if response_text.splitlines() else ""
    match = _RTSP_STATUS_LINE.match(first_line)
    return int(match.group(1)) if match else None


async def probe_camera_on_lan(
    camera: CameraRef, *, profile: StreamProfile = StreamProfile.SUB, timeout_s: float = 5.0,
) -> ReachabilityError | None:
    """None means reachable. Otherwise a classified `ReachabilityError` with
    `attempted_from=AttemptedFrom.LAN` -- never raises for a network
    failure, that failure IS the result."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(camera.host, camera.port), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.TIMEOUT,
            attempted_from=AttemptedFrom.LAN,
            detail=f"TCP connect to {camera.host}:{camera.port} timed out after {timeout_s}s",
        )
    except ConnectionRefusedError as exc:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.CONNECTION_REFUSED,
            attempted_from=AttemptedFrom.LAN, detail=str(exc),
        )
    except socket.gaierror as exc:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.DNS_FAILURE,
            attempted_from=AttemptedFrom.LAN, detail=str(exc),
        )
    except OSError as exc:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.UNKNOWN,
            attempted_from=AttemptedFrom.LAN, detail=str(exc),
        )

    try:
        url = camera.stream_url(profile)
        request = f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        writer.write(request.encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(1024), timeout=timeout_s)
    except asyncio.TimeoutError:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.TIMEOUT,
            attempted_from=AttemptedFrom.LAN,
            detail="TCP connected but the RTSP OPTIONS request never got a response",
        )
    except OSError as exc:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.UNKNOWN,
            attempted_from=AttemptedFrom.LAN, detail=str(exc),
        )
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout_s)
        except (OSError, asyncio.TimeoutError):
            pass

    text = raw.decode("utf-8", errors="replace")
    status = _parse_rtsp_status(text)

    if status in (401, 403):
        first_line = text.splitlines()[0] if text else ""
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.AUTH_FAILED,
            attempted_from=AttemptedFrom.LAN, detail=first_line,
        )
    if status is None:
        return ReachabilityError(
            camera_id=camera.camera_id, kind=ReachabilityErrorKind.CONNECTION_REFUSED,
            attempted_from=AttemptedFrom.LAN,
            detail="a port was open but it did not speak RTSP",
        )
    return None
