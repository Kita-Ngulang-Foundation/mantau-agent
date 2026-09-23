import asyncio
import threading
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest
from mantau_core.buffer import DurableSpool
from mantau_core.contracts import FallEvent
from mantau_core.detection import NullDetector

from mantau_agent.capabilities import CapabilityReport, InferenceMode as Mode, PlatformType
from mantau_agent.detect.router import InferenceRouter
from mantau_agent.detect.sampler import FrameSampler
from mantau_agent.uplink.client import UplinkClient
from mantau_agent.uplink.frames import FrameUplink
from mantau_agent.uplink.seq import SeqCounter
from mantau_agent.uplink.spool import EnvelopeSpool

FRAME = np.zeros((4, 4, 3), dtype=np.uint8)


class Detector:
    def __init__(self):
        self.calls = []
        self.closed = 0

    def push(self, image, ts):
        self.calls.append(ts)
        return [FallEvent(camera_id="cam", event_id=f"event-{ts}")]

    def close(self):
        self.closed += 1


class Events:
    def __init__(self):
        self.events = []

    async def send_event(self, event):
        self.events.append(event)


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

    def create(mode=Mode.EDGE, **kwargs):
        detector = kwargs.pop("detector", Detector())
        events = kwargs.pop("event_uplink", Events())
        cloud = kwargs.pop("inference_uplink", Cloud())
        live = []

        def handler(request):
            live.append(request)
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        frames = FrameUplink("http://server", "agent", "secret", "cam", client=client)
        capabilities = kwargs.pop("capabilities", CapabilityReport(
            platform=PlatformType.LINUX_X86_64, architecture="x86_64", cpu="test",
            cpu_count=4, memory_bytes=1024**3, software_version="test",
            detector_backend="mediapipe", supported_detector_backends=["null", "mediapipe"],
            detector_fps=20, cloud_available=cloud is not None,
        ))
        now = [0.0]
        router = InferenceRouter(
            camera_id="cam", detector=detector, event_uplink=events, frame_uplink=frames,
            capabilities=capabilities, mode=mode, inference_uplink=cloud,
            clock=lambda: now[0], **kwargs,
        )
        routers.append(router)
        return router, detector, events, cloud, live, now

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


async def test_edge_sends_events_only_even_when_live_view_enabled(rig):
    router, detector, events, cloud, live, now = rig()
    await router.start()
    await feed(router, now, [0, 100, 200])
    assert detector.calls == [0, 200]
    assert len(events.events) == 2
    assert cloud.calls == live == []


async def test_cloud_skips_detector_and_reuses_jpeg_encoder(rig):
    router, detector, events, cloud, live, now = rig(Mode.CLOUD, live_view_enabled=False)
    await router.start()
    await feed(router, now, [0, 500, 1000])
    assert detector.calls == events.events == live == []
    assert [m["ts_ms"] for _, m in cloud.calls] == [0, 1000]
    assert all(jpeg.startswith(b"\xff\xd8") for jpeg, _ in cloud.calls)
    assert all(m["event_ids"] == () for _, m in cloud.calls)


async def test_hybrid_confirms_real_events_at_explicit_limit(rig):
    router, detector, events, cloud, live, now = rig(Mode.HYBRID, live_view_enabled=False)
    await router.start()
    await feed(router, now, range(0, 10001, 200))
    assert len(detector.calls) == len(events.events) == 51
    assert [m["ts_ms"] for _, m in cloud.calls] == [0, 5000, 10000]
    assert cloud.calls[1][1]["event_ids"] == ("event-5000",)


async def test_hybrid_does_not_confirm_frames_without_events(rig):
    class Quiet(Detector):
        def push(self, image, ts):
            return []

    router, _, events, cloud, _, now = rig(Mode.HYBRID, detector=Quiet())
    await router.start()
    await feed(router, now, [0, 1000, 5000])
    assert events.events == cloud.calls == []


async def test_mediapipe_adapter_translation_is_reused(rig):
    from types import SimpleNamespace
    from mantau_core.detection.mediapipe_adapter import MediapipeDetector

    class StreamingImplementation(Detector):
        def push(self, image, ts):
            return [SimpleNamespace(confidence=.87, track_id=3, velocity=.5)]

        def process(self, image, ts):
            # The adapter is a PerceivingDetector, so the router calls perceive().
            return SimpleNamespace(events=self.push(image, ts), people=None)

    # Exercise the real core adapter with its missing optional model injected.
    detector = object.__new__(MediapipeDetector)
    detector.camera_id = "cam"
    detector._clock = lambda: datetime.now(timezone.utc)
    detector._impl = StreamingImplementation()
    router, _, events, _, _, now = rig(detector=detector)
    await router.start()
    await feed(router, now, [0])
    assert events.events[0].confidence == .87
    assert events.events[0].signals == {"velocity": .5}
    assert events.events[0].track_id == 3
    await router.shutdown()
    assert detector._impl.closed == 1


async def test_detection_cloud_and_live_rates_are_independent(rig):
    router, detector, events, cloud, live, now = rig(
        Mode.HYBRID, detection_fps=5, cloud_upload_fps=2, confirmation_fps=1, live_view_fps=4)
    await router.start()
    await feed(router, now, range(0, 1001, 50))
    assert len(detector.calls) == 6
    assert len(cloud.calls) == 2
    assert len(live) == 5


async def test_existing_sampler_only_decimates_detection(rig):
    router, detector, _, _, live, now = rig(
        Mode.HYBRID, sampler=FrameSampler(keep_every_n=3), live_view_fps=10)
    await router.start()
    await feed(router, now, range(0, 601, 100))
    assert detector.calls == [200, 500]
    assert len(live) == 7


async def test_auto_uses_measured_report_and_weak_device_falls_back(rig):
    strong, detector, _, _, _, now = rig(Mode.AUTO)
    await strong.start()
    await feed(strong, now, [0])
    assert strong.mode == Mode.EDGE
    assert detector.calls == [0]
    weak_report = strong.capabilities.model_copy(
        update={"detector_fps": 1.0, "cloud_available": True})
    weak, detector, _, cloud, _, now = rig(Mode.AUTO, capabilities=weak_report)
    await weak.start()
    await feed(weak, now, [0])
    assert weak.mode == Mode.CLOUD
    assert detector.calls == []
    assert len(cloud.calls) == 1


@pytest.mark.parametrize("mode", [Mode.EDGE, Mode.HYBRID])
async def test_null_synthetic_events_never_reach_production_uplink(rig, mode):
    router, _, events, cloud, _, now = rig(
        mode, detector=NullDetector("cam", trigger_every_n_frames=1), live_view_enabled=False)
    await router.start()
    await feed(router, now, [0, 200])
    assert router.health()["synthetic_events_suppressed"] == 2
    assert not router.detector_alive
    assert events.events == cloud.calls == []


async def test_synthetic_marker_from_any_backend_is_blocked(rig):
    class Synthetic(Detector):
        def push(self, image, ts):
            return [FallEvent(camera_id="cam", signals={"synthetic": 1.0})]

    router, _, events, cloud, _, now = rig(Mode.HYBRID, detector=Synthetic())
    await router.start()
    await feed(router, now, [0])
    assert events.events == cloud.calls == []


@pytest.mark.parametrize("mode,queues", [(Mode.EDGE, ["detection"]),
                                        (Mode.CLOUD, ["cloud", "live"])])
async def test_queues_are_bounded_and_drop_oldest(rig, mode, queues):
    router, detector, _, cloud, _, _ = rig(mode, queue_size=2)
    await router.start()
    # No yield: producers outrun consumers deterministically.
    for ts in range(0, 10000, 1000):
        router.submit(FRAME, ts)
    status = router.health()
    for name in queues:
        assert status["queue_depths"][name] == 2
        assert status["dropped_frames"][name] == 8
    await router.wait_idle()
    if mode == Mode.EDGE:
        assert detector.calls == [8000, 9000]
    else:
        assert cloud.calls[0][1]["ts_ms"] == 8000


@pytest.mark.parametrize("failure", [False, httpx.ConnectError("offline"), RuntimeError("adapter")])
async def test_failed_upload_is_counted_rate_limited_and_recovers(rig, failure):
    router, _, _, cloud, _, now = rig(Mode.HYBRID, live_view_enabled=False)
    cloud.outcome = failure
    await router.start()
    await feed(router, now, [0, 200, 400])
    assert len(cloud.calls) == 1
    assert router.upload_failures["cloud"] == 1
    cloud.outcome = True
    await feed(router, now, [5000])
    assert len(cloud.calls) == 2


async def test_slow_encoding_does_not_allow_upload_bursts(rig):
    router, _, _, cloud, _, now = rig(Mode.CLOUD, live_view_enabled=False)
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

    router, _, _, cloud, _, now = rig(Mode.CLOUD, inference_uplink=Hung(), upload_timeout_s=.02)
    await router.start()
    await feed(router, now, [0])
    assert router.upload_failures["cloud"] == 1
    await asyncio.wait_for(router.shutdown(), 1)
    assert cloud.closed == 1


async def test_missing_inference_adapter_is_degraded_and_never_uses_live_endpoint(rig):
    no_detector = CapabilityReport(
        platform=PlatformType.LINUX_X86_64, architecture="x86_64", cpu="test", cpu_count=4,
        memory_bytes=1024**3, software_version="test", detector_backend="mediapipe",
        supported_detector_backends=["null"], detector_error="failed")
    router, detector, _, _, live, now = rig(
        Mode.CLOUD, inference_uplink=None, live_view_enabled=False, capabilities=no_detector)
    await router.start()
    await feed(router, now, [0])
    assert router.mode == Mode.CLOUD
    assert router.health()["degraded"]
    assert not router.detector_alive
    assert live == []


async def test_cloud_without_server_inference_falls_back_to_the_local_detector(rig):
    router, detector, events, _, live, now = rig(
        Mode.CLOUD, inference_uplink=None, live_view_enabled=False)
    await router.start()
    await feed(router, now, [0])
    assert router.mode == Mode.EDGE and "falling back to EDGE" in router.selection.reason
    assert detector.calls == [0] and len(events.events) == 1


async def test_detector_that_dies_at_runtime_hands_over_to_cloud(rig):
    class Dying(Detector):
        def push(self, image, ts):
            self.calls.append(ts)
            raise RuntimeError("model crashed")

    router, detector, events, cloud, _, now = rig(
        Mode.EDGE, detector=Dying(), live_view_enabled=False)
    await router.start()
    await feed(router, now, [0, 1000, 2000])
    assert detector.calls == [0]
    assert router.mode == Mode.CLOUD and "RuntimeError" in router.selection.reason
    assert [m["ts_ms"] for _, m in cloud.calls] == [1000, 2000]
    assert events.events == []


async def test_mode_change_flushes_old_work_and_does_not_reset_upload_limit(rig):
    router, detector, _, cloud, live, now = rig(Mode.CLOUD, live_view_enabled=False)
    await router.start()
    await feed(router, now, [0])
    router.submit(FRAME, 1000)  # queued but not started
    await router.change_mode(Mode.EDGE)
    await feed(router, now, [1200])
    assert detector.calls == [1200]
    assert len(cloud.calls) == 1
    await router.change_mode(Mode.HYBRID)
    await feed(router, now, [1400])
    assert len(cloud.calls) == 1  # confirmation cap survives change
    assert live == []


async def test_mode_change_waits_for_active_upload(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Cloud):
        async def submit(self, jpeg, **metadata):
            entered.set()
            await release.wait()
            return await super().submit(jpeg, **metadata)

    router, detector, _, cloud, _, now = rig(
        Mode.CLOUD, inference_uplink=Blocking(), live_view_enabled=False)
    await router.start()
    router.submit(FRAME, 0)
    await asyncio.wait_for(entered.wait(), 1)
    change = asyncio.create_task(router.change_mode(Mode.EDGE))
    await asyncio.sleep(.01)
    assert not change.done()
    release.set()
    await asyncio.wait_for(change, 1)
    await feed(router, now, [1000])
    assert detector.calls == [1000]
    assert len(cloud.calls) == 1


async def test_shutdown_waits_for_active_detector_and_closes_once(rig):
    entered, release = threading.Event(), threading.Event()

    class Blocking(Detector):
        def push(self, image, ts):
            entered.set()
            assert release.wait(2)
            assert self.closed == 0
            return super().push(image, ts)

    router, detector, events, cloud, _, _ = rig(detector=Blocking())
    await router.start()
    await router.start()
    router.submit(FRAME, 0)
    assert await asyncio.to_thread(entered.wait, 1)
    stopped = asyncio.create_task(router.shutdown())
    await asyncio.sleep(.01)
    assert not stopped.done()
    release.set()
    await asyncio.wait_for(stopped, 2)
    await router.shutdown()
    assert detector.closed == cloud.closed == 1
    assert len(events.events) == 1
    router.submit(FRAME, 1000)
    assert not router.health()["running"]
    assert all(depth == 0 for depth in router.health()["queue_depths"].values())
    with pytest.raises(RuntimeError, match="closed"):
        await router.start()


async def test_events_survive_router_shutdown_in_real_durable_spool(rig, tmp_path):
    path = tmp_path / "events.db"
    spool = EnvelopeSpool(DurableSpool(path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        uplink = UplinkClient("http://server", "agent", "secret",
                              SeqCounter(tmp_path / "seq"), spool, client=client)
        router, _, _, _, _, now = rig(event_uplink=uplink)
        await router.start()
        await feed(router, now, [0])
        await router.shutdown()
        assert spool.depth() == 1
    spool.close()
    with DurableSpool(path) as reopened:
        pending = EnvelopeSpool(reopened).pending()
        assert pending[0].event().event_id == "event-0"
        assert pending[0].verify("secret")
