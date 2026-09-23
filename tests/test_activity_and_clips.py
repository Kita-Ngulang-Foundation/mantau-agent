import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from mantau_core.activity import ActivityEngine, FrameObservation, Perception
from mantau_core.contracts import (
    CommandState, CommandType, ControlCommand, DetectionSettings, EventKind, FallEvent,
)

from mantau_agent import clips as clips_module
from mantau_agent.clips import ClipRecorder, ClipUploader
from mantau_agent.config import Settings
from mantau_agent.control import CommandExecutor
from mantau_agent.state import (
    AgentConfiguration, ConfigurationStore, EnrollmentConfiguration, SetupState,
)

from test_inference_router import FRAME, Mode, feed, rig  # noqa: F401 -- fixture reuse


class FakeUploader:
    def __init__(self, outcome=True):
        self.outcome = outcome
        self.uploads = []

    async def upload(self, event_id, body):
        self.uploads.append((event_id, body))
        return self.outcome

    async def close(self):
        pass


def _recorder(tmp_path, uploader, **kwargs):
    counter = iter(range(10_000))
    return ClipRecorder(uploader=uploader, encode_jpeg=lambda image: f"jpeg-{next(counter)}".encode(),
                        spool_dir=tmp_path / "clips", fps=5, pre_s=1, post_s=1, **kwargs)


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(clips_module, "encode_mp4", lambda frames, fps: b"MP4:" + b"|".join(frames))


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


async def test_clip_spans_before_and_after_the_event(tmp_path):
    uploader = FakeUploader()
    recorder = _recorder(tmp_path, uploader)
    for ts in range(0, 2000, 100):  # 10 fps in, 5 fps kept
        recorder.add_frame(FRAME, ts)
    recorder.on_event(FallEvent(camera_id="cam", event_id="evt-1"))
    for ts in range(2000, 3200, 100):
        recorder.add_frame(FRAME, ts)
    await _settle()
    event_id, body = uploader.uploads[0]
    assert event_id == "evt-1"
    assert body.count(b"jpeg-") == 10  # 5 before + 5 after
    assert not list((tmp_path / "clips").glob("*.mp4"))


async def test_bathroom_events_are_never_recorded(tmp_path):
    uploader = FakeUploader()
    recorder = _recorder(tmp_path, uploader)
    recorder.on_event(FallEvent(camera_id="cam", kind=EventKind.BATHROOM_DURATION))
    for ts in range(0, 3000, 200):
        recorder.add_frame(FRAME, ts)
    await _settle()
    assert uploader.uploads == []


async def test_failed_upload_is_kept_and_retried_permanent_failure_dropped(tmp_path):
    uploader = FakeUploader(outcome=False)
    recorder = _recorder(tmp_path, uploader)
    recorder.on_event(FallEvent(camera_id="cam", event_id="evt-2"))
    for ts in range(0, 1400, 200):
        recorder.add_frame(FRAME, ts)
    await _settle()
    assert [p.name for p in (tmp_path / "clips").glob("*.mp4")] == ["evt-2.mp4"]

    uploader.outcome = True
    await recorder.retry_spooled()
    assert not list((tmp_path / "clips").glob("*.mp4"))
    assert recorder.uploaded == 1

    uploader.outcome = None
    recorder.on_event(FallEvent(camera_id="cam", event_id="evt-3"))
    for ts in range(2000, 3400, 200):
        recorder.add_frame(FRAME, ts)
    await _settle()
    assert not list((tmp_path / "clips").glob("*.mp4"))
    assert recorder.failed == 1


async def test_uploader_signs_the_clip_for_its_event():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(403 if "bath" in request.url.path else 204)

    uploader = ClipUploader("http://server/", "agent-1", "secret",
                            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await uploader.upload("evt-1", b"mp4") is True
    assert await uploader.upload("bath-1", b"mp4") is None
    assert seen[0].url.path == "/events/evt-1/recording"
    assert seen[0].headers["Content-Type"] == "video/mp4"
    import hashlib
    import hmac
    assert seen[0].headers["X-Mantau-Signature"] == hmac.new(
        b"secret", b"evt-1.mp4", hashlib.sha256).hexdigest()


class Perceiver:
    def __init__(self):
        self.closed = 0

    def push(self, image, ts):
        raise AssertionError("perceive() must be used")

    def perceive(self, image, ts):
        return Perception(
            events=[FallEvent(camera_id="cam", event_id=f"fall-{ts}")] if ts == 0 else [],
            observation=FrameObservation(camera_id="cam", at=datetime.now(timezone.utc)),
        )

    def close(self):
        self.closed += 1


class StillRule:
    kind = EventKind.STILLNESS

    def update(self, observation, settings):
        return [FallEvent(camera_id="cam", kind=self.kind, event_id="still-1")]

    def reset(self):
        pass


async def test_perceiving_detector_feeds_activity_rules_and_clips(rig, tmp_path):  # noqa: F811
    uploader = FakeUploader()
    recorder = _recorder(tmp_path, uploader)
    router, _, events, _, _, now = rig(
        detector=Perceiver(), activity=ActivityEngine([StillRule()]), clips=recorder)
    await router.start()
    await feed(router, now, [0])
    assert [e.event_id for e in events.events] == ["fall-0", "still-1"]
    assert {c.event_id for c in recorder._captures} == {"fall-0", "still-1"}
    assert router.health()["activity_rules"] == ["StillRule"]


def _command(payload):
    return ControlCommand(
        command_id="cmd-settings", command_type=CommandType.APPLY_DETECTION_SETTINGS,
        state=CommandState.DELIVERED, payload=payload,
        created_at="2026-09-22T08:00:00Z", expires_at="2099-09-22T08:05:00Z",
    )


async def test_detection_settings_are_applied_and_persisted(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.save(AgentConfiguration(
        setup_state=SetupState.COMPLETE,
        enrollment=EnrollmentConfiguration(server_url="http://s", agent_id="a", agent_secret="x"),
    ))
    engine = ActivityEngine()
    pipeline = SimpleNamespace(router=SimpleNamespace(activity=engine))
    executor = CommandExecutor(Settings(camera_id="cam-1"), pipeline, config_store=store)

    settings = DetectionSettings(version=4, bathroom={"warning_minutes": 12, "critical_minutes": 30})
    result = await executor.execute(_command(
        {"camera_id": "cam-1", "settings": settings.model_dump(mode="json")}))
    assert result.state is CommandState.SUCCEEDED
    assert result.data == {"camera_id": "cam-1", "detection_settings_version": 4}
    assert engine.settings.bathroom.warning_minutes == 12
    assert store.load().detection_settings.version == 4

    wrong_camera = await executor.execute(_command(
        {"camera_id": "cam-9", "settings": settings.model_dump(mode="json")}))
    assert wrong_camera.state is CommandState.FAILED
    invalid = await executor.execute(_command({"camera_id": "cam-1", "settings": {"timezone": "x"}}))
    assert invalid.state is CommandState.FAILED
    assert engine.settings.version == 4
