"""NullTunnel end-to-end, and TailscaleTunnel's missing-binary path -- the
one part of it genuinely testable without a real Tailscale install (and the
failure mode this sandbox itself would actually hit).
"""

import pytest

from mantau_agent.uplink.tunnel import NullTunnel, TailscaleTunnel


async def test_null_tunnel_is_always_up():
    tunnel = NullTunnel()
    await tunnel.up()
    status = await tunnel.status()
    assert status == {"provider": "null", "connected": True}
    await tunnel.down()  # must not raise


async def test_tailscale_tunnel_raises_a_clear_error_without_the_binary():
    tunnel = TailscaleTunnel(tailscale_bin="definitely-not-a-real-binary-xyz")
    with pytest.raises(RuntimeError, match="not found on PATH"):
        await tunnel.up()
    with pytest.raises(RuntimeError, match="not found on PATH"):
        await tunnel.down()


async def test_tailscale_tunnel_status_also_raises_without_the_binary():
    tunnel = TailscaleTunnel(tailscale_bin="definitely-not-a-real-binary-xyz")
    with pytest.raises(RuntimeError, match="not found on PATH"):
        await tunnel.status()
