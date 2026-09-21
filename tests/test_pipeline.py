import asyncio
import json

import httpx
import numpy as np
import pytest

from mantau_agent import main
from mantau_agent.capabilities import InferenceMode as Mode
from mantau_agent.config import Settings
from mantau_agent.discovery.onvif import OnvifDevice
from mantau_agent.uplink.client import UplinkClient
from mantau_agent.uplink.frames import FrameUplink


class Puller:
    def __init__(self, *args, **kwargs):
        self.started = self.stopped = 0
        self.reachable = False

    def start(self):
        self.started += 1
        self.reachable = True

    def latest_frame(self):
        return np.zeros((4, 4, 3), dtype=np.uint8), 0

    def stop(self):
        self.stopped += 1
        self.reachable = False


@pytest.fixture
async def pipeline_factory(monkeypatch, tmp_path):
    calls = []
    pipelines = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204 if request.url.path.endswith("/frame") else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(main, "CameraPuller", Puller)
        monkeypatch.setattr(main, "UplinkClient", lambda *a, **k: UplinkClient(*a, **k, client=client))
        monkeypatch.setattr(main, "FrameUplink", lambda *a, **k: FrameUplink(*a, **k, client=client))

        async def create(**overrides):
            settings = Settings(agent_id="agent", agent_secret="secret", camera_host="localhost",
                                seq_path=str(tmp_path / "seq"), spool_path=str(tmp_path / "spool.db"),
                                poll_interval_s=.001, heartbeat_interval_s=.02, **overrides)
            pipeline = await main.build_pipeline(settings)
            pipelines.append(pipeline)
            return pipeline, calls

        yield create
        for pipeline in pipelines:
            await pipeline.shutdown()


async def test_pipeline_starts_captures_reports_truthful_health_and_shuts_down(pipeline_factory):
    pipeline, calls = await pipeline_factory(inference_mode=Mode.EDGE, null_detector_trigger_every=1)
    await pipeline.start()
    await pipeline.start()
    for _ in range(100):
        if pipeline.router.synthetic_events_suppressed and calls:
            break
        await asyncio.sleep(.005)
    assert pipeline.puller.started == 1
    assert pipeline.router.synthetic_events_suppressed == 1
    assert all(request.url.path == "/ingest" for request in calls)
    heartbeats = [json.loads(request.content) for request in calls]
    assert heartbeats and all(h["kind"] == "heartbeat" for h in heartbeats)
    assert all(not h["payload"]["detector_alive"] for h in heartbeats)
    assert pipeline.health()["capabilities"]["detector_backend"] == "null"
    await pipeline.change_mode(Mode.CLOUD)
    assert pipeline.health()["routing"]["degraded"]
    await pipeline.shutdown()
    await pipeline.shutdown()
    assert pipeline.puller.stopped == 1
    assert all(task.done() for task in pipeline._tasks + pipeline.router._tasks)
    with pytest.raises(RuntimeError, match="closed"):
        await pipeline.start()


async def test_shutdown_before_start_releases_resources(pipeline_factory):
    pipeline, _ = await pipeline_factory()
    await pipeline.shutdown()
    assert all(v == 0 for v in pipeline.router.health()["queue_depths"].values())
    assert not pipeline.router.health()["running"]


async def test_failed_start_cleans_up_every_component(pipeline_factory):
    pipeline, _ = await pipeline_factory()
    calls = []

    class BrokenTunnel:
        async def up(self):
            raise RuntimeError("cannot start")

        async def down(self):
            calls.append("down")

    pipeline.tunnel = BrokenTunnel()
    with pytest.raises(RuntimeError, match="cannot start"):
        await pipeline.start()
    assert calls == ["down"]
    assert pipeline.puller.stopped == 1
    assert pipeline.router._closed
    await pipeline.shutdown()


async def test_auto_missing_mediapipe_degrades_without_creating_synthetic_detector(pipeline_factory):
    pipeline, _ = await pipeline_factory(detector_backend="mediapipe", inference_mode=Mode.AUTO)
    assert pipeline.router.mode == Mode.CLOUD
    assert pipeline.router.detector is None
    assert pipeline.router.capabilities.detector_error


async def test_explicit_cloud_does_not_construct_local_detector(pipeline_factory, monkeypatch):
    def forbidden(*args):
        raise AssertionError("CLOUD must not load a local model")

    monkeypatch.setattr(main, "_build_detector", forbidden)
    pipeline, _ = await pipeline_factory(inference_mode=Mode.CLOUD, detector_backend="mediapipe")
    assert pipeline.router.detector is None


@pytest.mark.parametrize("outcome,expected", [
    ([OnvifDevice(xaddrs=["http://192.0.2.42/onvif/device_service"])], "192.0.2.42"),
    ([], "manual-camera"), (OSError("no multicast"), "manual-camera"),
])
async def test_discovery_reuses_manual_contract_and_falls_back(monkeypatch, outcome, expected):
    def discover():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(main, "discover", discover)
    camera = await main._discover_camera(Settings(
        camera_host="manual-camera", camera_username="user", camera_password="pw",
        camera_sub_path="/sub", use_onvif_discovery=True,
    ))
    assert camera.host == expected
    assert camera.credentials.username == "user"
    assert "/sub" in camera.stream_url(main.StreamProfile.SUB)
