"""One interface, several implementations. Tailscale (WireGuard-based, free
tier) is the default for this weekend; frp (github.com/fatedier/frp) is the
documented self-hosted alternative, ngrok the fastest pure-demo option --
either could implement `TunnelProvider` later without changing anything
that calls it.

Only `NullTunnel` and `TailscaleTunnel`'s missing-binary path are exercised
by the test suite. Actually establishing a Tailscale connection needs a
real install and an authenticated account; there's no way to fake that
meaningfully without either the real binary or a mock so thin it would
prove nothing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Protocol


class TunnelProvider(Protocol):
    async def up(self) -> None:
        """Establish (or confirm) the tunnel. Idempotent."""
        ...

    async def status(self) -> dict:
        """Provider-specific shape (connected: bool, assigned address, ...)
        -- used for heartbeat/diagnostics, not for correctness."""
        ...

    async def down(self) -> None:
        ...


class NullTunnel:
    """Assumes the server is directly reachable -- no tunnel at all. For
    local dev (agent and server on the same machine/LAN) and for testing
    everything above this layer without a real Tailscale account."""

    async def up(self) -> None:
        pass

    async def status(self) -> dict:
        return {"provider": "null", "connected": True}

    async def down(self) -> None:
        pass


class TailscaleTunnel:
    """Shells out to the `tailscale` CLI. Requires Tailscale already
    installed AND authenticated (`tailscale up` run interactively once,
    outside this process) -- this class does not handle first-time OAuth.
    """

    def __init__(self, *, tailscale_bin: str = "tailscale") -> None:
        self._bin = tailscale_bin

    def _require_binary(self) -> None:
        if shutil.which(self._bin) is None:
            raise RuntimeError(
                f"{self._bin!r} not found on PATH -- install Tailscale first "
                f"(https://tailscale.com/download)."
            )

    async def up(self) -> None:
        self._require_binary()
        proc = await asyncio.create_subprocess_exec(
            self._bin, "up", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"`{self._bin} up` failed: {stderr.decode(errors='replace')}")

    async def status(self) -> dict:
        self._require_binary()
        proc = await asyncio.create_subprocess_exec(
            self._bin, "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"provider": "tailscale", "connected": False, "error": stderr.decode(errors="replace")}
        try:
            data = json.loads(stdout)
        except ValueError:
            return {"provider": "tailscale", "connected": False, "error": "unparseable status output"}
        self_node = data.get("Self", {})
        return {
            "provider": "tailscale",
            "connected": bool(self_node.get("Online", False)),
            "tailscale_ip": (self_node.get("TailscaleIPs") or [None])[0],
        }

    async def down(self) -> None:
        self._require_binary()
        proc = await asyncio.create_subprocess_exec(
            self._bin, "down", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
