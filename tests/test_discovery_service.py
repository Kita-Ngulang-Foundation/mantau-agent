from mantau_core.contracts import CameraRef, ReachabilityError, ReachabilityErrorKind, StreamProfile

from mantau_agent.discovery.onvif import OnvifDevice
from mantau_agent.discovery.service import deduplicate_devices, discover_cameras


CAMERA = CameraRef(
    camera_id="cam", name="cam", host="manual",
    paths={StreamProfile.MAIN: "/main", StreamProfile.SUB: "/low"},
)


def test_deduplicates_same_host_and_merges_metadata():
    devices = deduplicate_devices([
        OnvifDevice(xaddrs=["http://CAMERA.local/onvif/a"], types="type-a"),
        OnvifDevice(xaddrs=["http://camera.local/onvif/b"], scopes="scope-b"),
        OnvifDevice(xaddrs=[]),
    ])
    assert len(devices) == 1
    assert devices[0].xaddrs == ["http://CAMERA.local/onvif/a", "http://camera.local/onvif/b"]
    assert devices[0].types == "type-a"
    assert devices[0].scopes == "scope-b"


async def test_discovery_probes_every_unique_host_using_preferred_substream():
    probed = []

    def discover(*, timeout_s):
        return [
            OnvifDevice(xaddrs=["http://one/onvif"]),
            OnvifDevice(xaddrs=["http://one/onvif/duplicate"]),
            OnvifDevice(xaddrs=["http://two/onvif"]),
        ]

    async def probe(camera, *, profile, timeout_s):
        probed.append((camera.host, profile))
        if camera.host == "two":
            return ReachabilityError(
                camera_id="cam", kind=ReachabilityErrorKind.TIMEOUT,
                attempted_from="lan", detail="timeout")
        return None

    candidates = await discover_cameras(
        CAMERA, profile=StreamProfile.SUB, discover_fn=discover, probe_fn=probe)
    assert probed == [("one", StreamProfile.SUB), ("two", StreamProfile.SUB)]
    assert [(c.host, c.rtsp_reachable, c.reachability_error) for c in candidates] == [
        ("one", True, None), ("two", False, "timeout")]
