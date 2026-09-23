"""HttpInferenceUplink (server inference) and the automatic EDGE -> CLOUD fallback.

Wire-level tests use httpx.MockTransport and check the real signature with
mantau-core's verifier. The round-trip tests at the end run the actual
mantau-server app in-process (skipped when it is not installed) with a fake
detector, so agent and server are held to the same contract.
"""

from __future__ import annotations

import json
import time

import httpx
import numpy as np
import pytest
from mantau_core.contracts import FallEvent, InferenceCapability, InferenceResult
from mantau_core.contracts import inference as contract

from mantau_agent import main
from mantau_agent.capabilities import InferenceMode as Mode
from mantau_agent.config import Settings
from mantau_agent.uplink.inference import HttpInferenceUplink, cloud_rate, discover_capability

SECRET = "agent-secret"
CAPABILITY = InferenceCapability(available=True, detector="mediapipe", max_frame_bytes=1024,
                                 max_frame_age_s=10.0, max_fps=15.0)


def _result(request: httpx.Request, **changes) -> httpx.Response:
    body = InferenceResult(frame_id=request.headers["X-Mantau-Frame"],
                           session_id=request.headers["X-Mantau-Session"], processed=True,
                           **changes)
    return httpx.Response(200, json=body.model_dump(mode="json"))


def _verified(request: httpx.Request) -> bool:
    h = request.headers
    event_ids = [e for e in h.get("X-Mantau-Event-Ids", "").split(",") if e]
    return contract.verify(SECRET, h["X-Mantau-Signature"], agent_id=h["X-Mantau-Agent"],
                           camera_id=h["X-Mantau-Camera"], session_id=h["X-Mantau-Session"],
                           frame_id=h["X-Mantau-Frame"], ts_ms=int(h["X-Mantau-Frame-Ts"]),
                           captured_at_ms=int(h["X-Mantau-Captured-At"]), event_ids=event_ids,
                           body=request.content)


def _uplink(handler, **kwargs) -> tuple[HttpInferenceUplink, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("retry_base_s", 0.001)
    return HttpInferenceUplink("http://server/", "agent-1", SECRET, CAPABILITY, client=client,
                               **kwargs), client


async def test_success_is_signed_correlated_and_reports_server_events():
    seen, delivered = [], []
    event = FallEvent(camera_id="cam-1", confidence=0.9)

    def handler(request):
        seen.append(request)
        return _result(request, events=[event])

    uplink, client = _uplink(handler, on_events=delivered.extend)
    async with client:
        assert await uplink.submit(b"\xff\xd8jpeg", camera_id="cam-1", ts_ms=500,
                                   event_ids=("e1", "e2")) is True
    request = seen[0]
    assert request.url.path == "/agents/agent-1/inference"
    assert request.headers["Content-Type"] == "image/jpeg"
    assert request.headers["X-Mantau-Event-Ids"] == "e1,e2"
    assert request.headers["X-Mantau-Frame-Ts"] == "500"
    assert _verified(request)
    assert [e.event_id for e in delivered] == [event.event_id]
    assert uplink.stats["accepted"] == 1 and uplink.stats["events"] == 1


@pytest.mark.parametrize("status", [400, 401, 404, 413, 422, 429])
async def test_client_errors_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "x"})

    uplink, client = _uplink(handler)
    async with client:
        assert await uplink.submit(b"jpeg", camera_id="cam-1", ts_ms=1) is False
    assert len(calls) == 1 and uplink.stats["failed"] == 1
    assert uplink.last_error == f"HTTP {status}"


async def test_server_error_and_timeout_are_retried_with_the_same_frame_id():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("slow")
        if len(calls) == 2:
            return httpx.Response(503, json={"detail": "inference_capacity"})
        return _result(request)

    uplink, client = _uplink(handler, retries=2)
    async with client:
        assert await uplink.submit(b"jpeg", camera_id="cam-1", ts_ms=1) is True
    assert len(calls) == 3
    assert len({r.headers["X-Mantau-Frame"] for r in calls}) == 1
    assert all(_verified(r) for r in calls[1:])
    assert uplink.stats["retried"] == 2


async def test_retries_stop_and_the_frame_is_dropped():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502)

    uplink, client = _uplink(handler, retries=1)
    async with client:
        assert await uplink.submit(b"jpeg", camera_id="cam-1", ts_ms=1) is False
    assert len(calls) == 2 and uplink.stats["failed"] == 1


async def test_stale_frames_are_not_retried():
    now = [1000.0]
    calls = []

    def handler(request):
        calls.append(request)
        now[0] += 11  # the first attempt took longer than max_frame_age_s
        return httpx.Response(503)

    uplink, client = _uplink(handler, retries=3, wall_clock=lambda: now[0])
    async with client:
        assert await uplink.submit(b"jpeg", camera_id="cam-1", ts_ms=1) is False
    assert len(calls) == 1


async def test_oversized_frames_are_never_sent():
    uplink, client = _uplink(lambda r: pytest.fail("must not send"))
    async with client:
        assert await uplink.submit(b"x" * 1025, camera_id="cam-1", ts_ms=1) is False
    assert uplink.stats["too_large"] == 1


def test_capture_time_follows_the_stream_clock():
    now = [100.3]
    uplink = HttpInferenceUplink("http://s", "a", SECRET, CAPABILITY, wall_clock=lambda: now[0])
    # First frame sent 300 ms after capture: estimate is 300 ms late at worst.
    assert uplink.captured_at_ms(1000) == 100_300
    now[0] = 100.9  # this one waited in the queue: its capture time is not "now"
    assert uplink.captured_at_ms(1100) == 100_400
    now[0] = 100.45  # sent within 250 ms of capture: the offset estimate tightens
    assert uplink.captured_at_ms(1200) == 100_450
    now[0] = 101.0
    assert uplink.captured_at_ms(1300) == 100_550


def test_rate_is_capped_by_the_server():
    assert cloud_rate(10.0, CAPABILITY) == 10.0
    assert cloud_rate(30.0, CAPABILITY) == 15.0


async def test_capability_discovery():
    def handler(request):
        assert request.url.path == "/inference/capability"
        return httpx.Response(200, json=CAPABILITY.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await discover_capability("http://server", client=client) == CAPABILITY
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(404))) as client:
        assert await discover_capability("http://server", client=client) is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))) as client:
        assert await discover_capability("http://server", client=client) is None


# -- automatic fallback at startup ---------------------------------------------------

class _Puller:
    def __init__(self, *args, **kwargs):
        self.reachable = False

    def start(self):
        self.reachable = True

    def latest_frame(self):
        return None

    def stop(self):
        self.reachable = False


@pytest.fixture
async def build(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "CameraPuller", _Puller)
    monkeypatch.setattr("mantau_agent.capabilities._memory_bytes", lambda: 2 * 1024**3)
    pipelines = []

    async def create(*, capability=CAPABILITY, detector=None, detector_error=None, **overrides):
        async def discover(*args, **kwargs):
            return capability

        def build_detector(settings):
            if detector_error is not None:
                raise detector_error
            return detector

        monkeypatch.setattr(main, "discover_capability", discover)
        monkeypatch.setattr(main, "_build_detector", build_detector)
        settings = Settings(agent_id="agent-1", agent_secret=SECRET, camera_host="localhost",
                            server_url="http://127.0.0.1:9", seq_path=str(tmp_path / "seq"),
                            spool_path=str(tmp_path / "spool.db"),
                            status_path=str(tmp_path / "status.json"),
                            detector_backend="mediapipe", clips_enabled=False, **overrides)
        pipeline = await main.build_pipeline(settings)
        pipelines.append(pipeline)
        return pipeline

    yield create
    for pipeline in pipelines:
        await pipeline.shutdown()


class _Detector:
    def __init__(self, fps: float):
        self.fps = fps

    def benchmark(self):
        return self.fps

    def push(self, frame, ts):
        return []

    def close(self):
        pass


@pytest.mark.parametrize("requested", [Mode.AUTO, Mode.EDGE, Mode.HYBRID])
async def test_edge_model_that_fails_to_load_falls_back_to_cloud(build, requested):
    from mantau_core.detection.artifacts import ArtifactError
    pipeline = await build(detector_error=ArtifactError("model artifact does not match"),
                           inference_mode=requested)
    router = pipeline.router
    assert router.mode == Mode.CLOUD
    assert isinstance(router.inference_uplink, HttpInferenceUplink)
    assert router.detector is None
    assert "ArtifactError" in router.capabilities.detector_error
    assert router.capabilities.supported_inference_modes == [Mode.AUTO, Mode.CLOUD]


async def test_edge_model_that_benchmarks_too_slow_falls_back_to_cloud(build):
    pipeline = await build(detector=_Detector(fps=2.0), inference_mode=Mode.AUTO,
                           detection_fps=15.0)
    assert pipeline.router.mode == Mode.CLOUD
    assert "throughput" in pipeline.router.selection.reason
    assert pipeline.router.cloud_upload_fps == 10.0


async def test_fast_edge_model_stays_on_device_with_cloud_available(build):
    pipeline = await build(detector=_Detector(fps=40.0), inference_mode=Mode.AUTO)
    assert pipeline.router.mode == Mode.EDGE
    assert Mode.HYBRID in pipeline.router.capabilities.supported_inference_modes


async def test_without_server_inference_a_broken_edge_model_stays_visible(build):
    unavailable = CAPABILITY.model_copy(update={"available": False, "reason": "disabled"})
    pipeline = await build(capability=unavailable, detector_error=RuntimeError("x"),
                           inference_mode=Mode.AUTO)
    assert pipeline.router.inference_uplink is None
    assert pipeline.router.health()["degraded"]
    with pytest.raises(RuntimeError):
        await build(capability=unavailable, detector_error=RuntimeError("x"),
                    inference_mode=Mode.EDGE)


async def test_server_rate_caps_the_cloud_upload_rate(build):
    slow_server = CAPABILITY.model_copy(update={"max_fps": 4.0})
    pipeline = await build(capability=slow_server, detector_error=RuntimeError("x"))
    assert pipeline.router.cloud_upload_fps == 4.0


# -- round trip against the real server app -----------------------------------------

class _ServerDetector:
    """Server-side fake: a frame whose first pixel is 255 is a fall."""

    def __init__(self, camera_id, config, clock):
        self.camera_id, self.config, self.clock = camera_id, config, clock

    def perceive(self, image, ts_ms):
        from mantau_core.activity import FrameObservation, Perception, PersonObservation, Posture
        lying = bool(image[0, 0, 0] > 200)
        person = PersonObservation(track_id=1, bbox=(0.1, 0.5, 0.6, 0.3),
                                   posture=Posture.LYING if lying else Posture.STANDING,
                                   confidence=0.8)
        events = ([FallEvent(camera_id=self.camera_id, occurred_at=self.clock(), confidence=0.9)]
                  if lying and not self.config else [])
        return Perception(events=events, observation=FrameObservation(
            camera_id=self.camera_id, at=self.clock(), people=(person,)))

    def close(self):
        pass


@pytest.fixture
async def server():
    pytest.importorskip("mantau_ld.api.app")
    from mantau_ld.api.app import create_app
    from mantau_ld.config import Settings as ServerSettings

    app = create_app(ServerSettings(db_path=":memory:", control_plane_mode="local_dev",
                                    inference_max_fps=1000),
                     inference_factory=_ServerDetector)
    user = {"X-Mantau-User-ID": "family"}
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        enrolled = (await client.post("/agents/enroll", json={"agent_id": "agent-1"})).json()
        assert (await client.post("/agent-claims", headers=user, json={
            "claim_code": enrolled["claim_code"], "platform": "linux_x86_64"})).status_code == 200
        assert (await client.post("/cameras", headers=user, json={
            "camera_id": "cam-1", "name": "Kamar", "agent_id": "agent-1"})).status_code == 201
        yield client, enrolled["secret"], user


def _jpeg(fall: bool) -> bytes:
    import cv2
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    if fall:
        image[:8, :8] = 255
    return cv2.imencode(".jpg", image)[1].tobytes()


async def test_cloud_round_trip_produces_a_stored_fall_event(server):
    client, secret, user = server
    capability = await discover_capability("http://server", client=client)
    assert capability.available
    delivered = []
    uplink = HttpInferenceUplink("http://server", "agent-1", secret, capability, client=client,
                                 on_events=delivered.extend)
    started = time.perf_counter()
    assert await uplink.submit(_jpeg(False), camera_id="cam-1", ts_ms=0)
    assert await uplink.submit(_jpeg(True), camera_id="cam-1", ts_ms=100)
    assert (time.perf_counter() - started) < 5.0
    assert len(delivered) == 1
    stored = (await client.get(f"/events/{delivered[0].event_id}", headers=user)).json()
    assert stored["camera_id"] == "cam-1" and stored["signals"]["server_inference"] == 1.0


async def test_hybrid_confirmation_round_trip(server):
    from mantau_core.buffer import DurableSpool
    from mantau_agent.uplink.client import UplinkClient
    from mantau_agent.uplink.seq import SeqCounter
    from mantau_agent.uplink.spool import EnvelopeSpool

    client, secret, user = server
    capability = await discover_capability("http://server", client=client)
    # The agent's own detector found a fall and sent it the normal way...
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        spool = EnvelopeSpool(DurableSpool(f"{tmp}/spool.db", ttl_s=300))
        events = UplinkClient("http://server", "agent-1", secret, SeqCounter(f"{tmp}/seq"),
                              spool, client=client)
        local = FallEvent(camera_id="cam-1", confidence=0.7)
        await events.send_event(local)
        spool.close()
    # ...then HYBRID asks the server to confirm it from one frame.
    uplink = HttpInferenceUplink("http://server", "agent-1", secret, capability, client=client)
    assert await uplink.submit(_jpeg(True), camera_id="cam-1", ts_ms=5000,
                               event_ids=(local.event_id,))
    confirmation = uplink.last_result.confirmations[0]
    assert confirmation.event_id == local.event_id and confirmation.confirmed
    stored = (await client.get(f"/events/{local.event_id}", headers=user)).json()
    assert stored["server_confirmed"] is True


async def test_tampered_upload_is_rejected_by_the_real_server(server):
    client, _, _ = server
    capability = await discover_capability("http://server", client=client)
    uplink = HttpInferenceUplink("http://server", "agent-1", "not-the-secret", capability,
                                 client=client)
    assert await uplink.submit(_jpeg(True), camera_id="cam-1", ts_ms=1) is False
    assert uplink.last_error == "HTTP 401"
    payload = json.dumps(uplink.health())
    assert "not-the-secret" not in payload
