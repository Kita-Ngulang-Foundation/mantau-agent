import hashlib
import hmac

import httpx
import numpy as np
import pytest

from mantau_agent.uplink.frames import FrameUplink


async def test_live_frame_signature_and_endpoint_remain_compatible():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uplink = FrameUplink("http://server", "agent", "secret", "cam", client=client)
        jpeg = uplink.encode(np.zeros((10, 10, 3), dtype=np.uint8))
        assert await uplink.push(jpeg)
    request = calls[0]
    assert request.url.path == "/cameras/cam/frame"
    assert request.headers["X-Mantau-Signature"] == hmac.new(
        b"secret", b"cam." + jpeg, hashlib.sha256).hexdigest()
    assert request.headers["X-Mantau-Agent"] == "agent"


@pytest.mark.parametrize("failure", [500, 401, httpx.ConnectError("offline")])
async def test_live_upload_failure_is_a_false_result(failure):
    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uplink = FrameUplink("http://server", "agent", "secret", "cam", client=client)
        assert not await uplink.push(b"jpeg")
