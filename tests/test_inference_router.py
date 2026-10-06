import asyncio

import httpx
import numpy as np
import pytest

from mantau_agent.capabilities import CapabilityReport, InferenceMode as Mode, PlatformType
from mantau_agent.detect.router import InferenceRouter
from mantau_agent.uplink.frames import FrameUplink

FRAME = np.zeros((4, 4, 3), dtype=np.uint8)


class Cloud:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.outcome = True

    async def submit(self, jpeg, **metadata):
        self.calls.append((jpeg, metadata))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def close(self):
        self.closed += 1


@pytest.fixture
async def rig():
    routers, clients = [], []

    def create(mode=Mode.CLOUD, **kwargs):
        cloud = kwargs.pop("inference_uplink", Cloud())
        kwargs.setdefault("cloud_upload_fps", 1.0)
        viewers = kwargs.pop("viewers", 1)
        live = []

        def handler(request):
            live.append(request)
            return httpx.Response(204, headers={"X-Mantau-Live-Viewers": str(viewers)})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        frames = FrameUplink("http://server", "agent", "secret", "cam", client=client)
        capabilities = kwargs.pop("capabilities", CapabilityReport(
            platform=PlatformType.LINUX_X86_64, architecture="x86_64", cpu="test",
            cpu_count=4, memory_bytes=1024**3, software_version="test",
            cloud_available=cloud is not None,
        ))
        now = [0.0]
        router = InferenceRouter(
            camera_id="cam", frame_uplink=frames,
            capabilities=capabilities, mode=mode, inference_uplink=cloud,
            clock=lambda: now[0], **kwargs,
        )
        routers.append(router)
        return router, cloud, live, now

    yield create
    for router in routers:
        await router.shutdown()
    for client in clients:
        await client.aclose()


async def feed(router, now, timestamps):
    for ts in timestamps:
        now[0] = ts / 1000
        router.submit(FRAME, ts)
        await router.wait_idle()


async def test_cloud_uploads_sampled_frames_and_reuses_jpeg_encoder(rig):
    router, cloud, live, now = rig(Mode.CLOUD, live_view_enabled=False)
    await router.start()
    await feed(router, now, [0, 500, 1000])
    assert live == []
    assert [m["ts_ms"] for _, m in cloud.calls] == [0, 1000]
    assert all(jpeg.startswith(b"\xff\xd8") for jpeg, _ in cloud.calls)


async def test_cloud_and_live_rates_are_independent(rig):
    router, cloud, live, now = rig(Mode.CLOUD, cloud_upload_fps=2, live_view_fps=4)
    await router.start()
    await feed(router, now, range(0, 1001, 50))
    assert len(cloud.calls) == 3
    assert len(live) == 5


async def test_queues_are_bounded_and_drop_oldest(rig):
    router, cloud, _, _ = rig(Mode.CLOUD, queue_size=2)
    await router.start()
    # No yield: producers outrun consumers deterministically.
    for ts in range(0, 10000, 1000):
        router.submit(FRAME, ts)
    status = router.health()
    for name in ["cloud", "live"]:
        assert status["queue_depths"][name] == 2
        assert status["dropped_frames"][name] == 8
    await router.wait_idle()
    assert cloud.calls[0][1]["ts_ms"] == 8000


@pytest.mark.parametrize("failure", [False, httpx.ConnectError("offline"), RuntimeError("adapter")])
async def test_failed_upload_is_counted_rate_limited_and_recovers(rig, failure):
    router, cloud, _, now = rig(Mode.CLOUD, live_view_enabled=False)
    cloud.outcome = failure
    await router.start()
    await feed(router, now, [0, 200, 400])
    assert len(cloud.calls) == 1
    assert router.upload_failures["cloud"] == 1
    cloud.outcome = True
    await feed(router, now, [5000])
    assert len(cloud.calls) == 2


async def test_slow_encoding_does_not_allow_upload_bursts(rig):
    router, cloud, _, now = rig(Mode.CLOUD, live_view_enabled=False)
    encode = router.frame_uplink.encode

    def slow_encode(image):
        now[0] += .75
        return encode(image)

    router.frame_uplink.encode = slow_encode
    await router.start()
    await feed(router, now, [0, 1000, 2000])
    assert [m["ts_ms"] for _, m in cloud.calls] == [0, 2000]


async def test_hung_upload_times_out_and_shutdown_finishes(rig):
    class Hung(Cloud):
        async def submit(self, jpeg, **metadata):
            await asyncio.Event().wait()

    router, cloud, _, now = rig(Mode.CLOUD, inference_uplink=Hung(), upload_timeout_s=.02)
    await router.start()
    await feed(router, now, [0])
    assert router.upload_failures["cloud"] == 1
    await asyncio.wait_for(router.shutdown(), 1)
    assert cloud.closed == 1


async def test_missing_inference_adapter_is_degraded_and_never_uses_live_endpoint(rig):
    no_server = CapabilityReport(
        platform=PlatformType.LINUX_X86_64, architecture="x86_64", cpu="test", cpu_count=4,
        memory_bytes=1024**3, software_version="test", cloud_available=False)
    router, _, live, now = rig(
        Mode.CLOUD, inference_uplink=None, live_view_enabled=False, capabilities=no_server)
    await router.start()
    await feed(router, now, [0])
    assert router.mode == Mode.CLOUD
    assert router.health()["degraded"]
    assert not router.detector_alive
    assert live == []


async def test_mode_change_keeps_cloud_uploading(rig):
    router, cloud, live, now = rig(Mode.CLOUD, live_view_enabled=False)
    await router.start()
    await feed(router, now, [0])
    await router.change_mode(Mode.EDGE)
    await feed(router, now, [1200])
    assert router.mode == Mode.CLOUD
    await router.change_mode(Mode.HYBRID)
    await feed(router, now, [2400])
    assert len(cloud.calls) == 3
    assert live == []


async def test_mode_change_does_not_interrupt_active_upload(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Cloud):
        async def submit(self, jpeg, **metadata):
            entered.set()
            await release.wait()
            return await super().submit(jpeg, **metadata)

    router, cloud, _, now = rig(
        Mode.CLOUD, inference_uplink=Blocking(), live_view_enabled=False)
    await router.start()
    router.submit(FRAME, 0)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(router.change_mode(Mode.EDGE), 1)
    release.set()
    await feed(router, now, [1000])
    assert router.mode == Mode.CLOUD
    assert len(cloud.calls) == 2

async def test_live_view_idles_until_someone_watches(rig):
    router, _, live, now = rig(Mode.CLOUD, live_view_fps=10, viewers=0)
    await router.start()
    await feed(router, now, range(0, 2001, 100))
    assert len(live) == 3

async def test_post_start_cloud_loss_and_recovery_changes_protection(rig):
    router, cloud, _, now = rig()
    await router.start()
    assert router.health()['degraded']
    await feed(router, now, [0])
    assert not router.health()['degraded']
    cloud.outcome = False
    await feed(router, now, [1000])
    assert router.health()['degraded']
    cloud.outcome = True
    await feed(router, now, [2000])
    assert not router.health()['degraded']
    now[0] = 18
    assert router.health()['degraded']
