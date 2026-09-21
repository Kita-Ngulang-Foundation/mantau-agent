"""Deduplicated ONVIF inventory with LAN RTSP reachability results."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mantau_core.contracts import CameraRef, StreamProfile
from pydantic import BaseModel, Field

from .onvif import OnvifDevice, discover
from .probe import probe_camera_on_lan


class DiscoveryCandidate(BaseModel):
    host: str
    xaddrs: list[str] = Field(default_factory=list)
    types: str = ""
    scopes: str = ""
    rtsp_reachable: bool
    reachability_error: str | None = None


def deduplicate_devices(devices: list[OnvifDevice]) -> list[OnvifDevice]:
    merged: dict[str, OnvifDevice] = {}
    for device in devices:
        host = device.host
        if not host:
            continue
        key = host.casefold()
        if key not in merged:
            merged[key] = OnvifDevice(
                xaddrs=list(dict.fromkeys(device.xaddrs)),
                types=device.types, scopes=device.scopes,
            )
            continue
        current = merged[key]
        current.xaddrs = list(dict.fromkeys([*current.xaddrs, *device.xaddrs]))
        if device.types and device.types not in current.types:
            current.types = " ".join(filter(None, (current.types, device.types)))
        if device.scopes and device.scopes not in current.scopes:
            current.scopes = " ".join(filter(None, (current.scopes, device.scopes)))
    return list(merged.values())


async def discover_cameras(
    template: CameraRef, *, profile: StreamProfile = StreamProfile.SUB,
    timeout_s: float = 3.0,
    discover_fn: Callable[..., list[OnvifDevice]] = discover,
    probe_fn=probe_camera_on_lan,
) -> list[DiscoveryCandidate]:
    devices = deduplicate_devices(await asyncio.to_thread(discover_fn, timeout_s=timeout_s))

    async def inspect(device: OnvifDevice) -> DiscoveryCandidate:
        host = device.host
        camera = template.model_copy(update={"host": host})
        error = await probe_fn(camera, profile=profile, timeout_s=timeout_s)
        return DiscoveryCandidate(
            host=host, xaddrs=device.xaddrs, types=device.types, scopes=device.scopes,
            rtsp_reachable=error is None,
            reachability_error=error.kind.value if error is not None else None,
        )

    return list(await asyncio.gather(*(inspect(device) for device in devices)))
