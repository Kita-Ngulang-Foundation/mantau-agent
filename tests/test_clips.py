import asyncio
import os
import threading

import httpx
import pytest

from mantau_core.contracts import FallEvent
from mantau_agent import clips as clips_module
from mantau_agent.clips import ClipRecorder, ClipUploader


class Uploader:
    def __init__(self, result=False):
        self.result = result
        self.uploads = []

    async def upload(self, event_id, body):
        self.uploads.append((event_id, body))
        return self.result

    async def close(self):
        pass


def recorder(directory, uploader=None, **limits):
    return ClipRecorder(spool_dir=directory, uploader=uploader or Uploader(),
                        encode_jpeg=lambda image: image, fps=1, pre_s=1, post_s=1, **limits)


async def finish_event(subject, event_id="event-1"):
    subject.on_event(FallEvent(camera_id="camera-1", event_id=event_id))
    subject.add_frame(b"jpeg", 1000)
    await asyncio.gather(*tuple(subject._tasks))


async def test_event_before_clip_upload_is_retried_after_recorder_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(clips_module, "encode_mp4", lambda frames, fps: b"valid-mp4")
    accepted_event = False
    uploads = []

    def server(request):
        uploads.append(request.content)
        return httpx.Response(204 if accepted_event else 404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        original = recorder(tmp_path, ClipUploader("http://server", "agent-1", "secret", client=client))
        await finish_event(original)
        assert (tmp_path / "event-1.mp4").read_bytes() == b"valid-mp4"
        assert original.health()["retry_failures"] == 1
        await original.close()

        accepted_event = True
        recovered = recorder(tmp_path, ClipUploader("http://server", "agent-1", "secret", client=client))
        assert recovered.health()["spooled"] == 1
        await recovered.retry_spooled()
        await recovered.retry_spooled()
        assert recovered.uploaded == 1
        assert recovered.health()["spooled"] == 0
        assert uploads == [b"valid-mp4", b"valid-mp4"]
        await recovered.close()


def test_byte_capacity_evicts_oldest_and_rejects_oversized_clip(tmp_path):
    subject = recorder(tmp_path, max_spool_bytes=10)
    subject._store("old", b"123456")
    os.utime(tmp_path / "old.mp4", (100, 100))
    # Keep the old file eligible for capacity eviction rather than expiry.
    subject._max_spool_age_s = 10**12
    subject._store("new", b"abcdef")
    assert not (tmp_path / "old.mp4").exists()
    assert subject.health()["evicted_capacity"] == 1
    assert subject.health()["spooled_bytes"] == 6
    with pytest.raises(ValueError, match="spool byte limit"):
        subject._store("huge", b"x" * 11)
    assert (tmp_path / "new.mp4").read_bytes() == b"abcdef"
    assert list(tmp_path.glob("*.part")) == []


async def test_expiry_and_partial_cleanup_survive_process_restart(tmp_path):
    (tmp_path / "expired.mp4").write_bytes(b"expired")
    os.utime(tmp_path / "expired.mp4", (900, 900))
    (tmp_path / "recent.mp4").write_bytes(b"recent")
    os.utime(tmp_path / "recent.mp4", (990, 990))
    (tmp_path / "crashed.part").write_bytes(b"incomplete")
    now = [1000.0]
    uploader = Uploader()
    recovered = recorder(tmp_path, uploader, max_spool_age_s=60, wall_clock=lambda: now[0])
    assert recovered.health()["expired"] == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["recent.mp4"]
    now[0] = 1100.0
    await recovered.retry_spooled()
    assert recovered.health()["expired"] == 2
    assert uploader.uploads == []
    await recovered.close()


async def test_persistence_failure_is_reported_without_losing_capture_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(clips_module, "encode_mp4", lambda frames, fps: b"mp4")
    subject = recorder(tmp_path)

    def full_disk(event_id, body):
        raise OSError("disk full")

    monkeypatch.setattr(subject, "_store", full_disk)
    await finish_event(subject)
    assert subject.health()["persistence_failures"] == 1
    assert subject.health()["last_error"] == "persistence: OSError"
    assert subject.uploader.uploads == []
    await subject.close()


async def test_camera_clear_discards_encoding_that_completes_late(tmp_path, monkeypatch):
    started, resume = threading.Event(), threading.Event()

    def slow_encoder(frames, fps):
        started.set()
        assert resume.wait(5)
        return b"old-camera-clip"

    monkeypatch.setattr(clips_module, "encode_mp4", slow_encoder)
    subject = recorder(tmp_path)
    subject.on_event(FallEvent(camera_id="old-camera", event_id="old-event"))
    subject.add_frame(b"old-camera-jpeg", 1000)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        subject.clear_capture()
    finally:
        resume.set()
        await asyncio.gather(*tuple(subject._tasks))
    assert subject.health()["spooled"] == 0
    assert subject.uploader.uploads == []
    assert list(subject._ring) == []
    await subject.close()


def test_new_camera_can_restart_timestamps_and_duplicate_events_are_bounded(tmp_path):
    subject = recorder(tmp_path, max_pending_captures=1)
    subject.add_frame(b"old", 100_000)
    event = FallEvent(camera_id="old", event_id="same-event")
    subject.on_event(event)
    subject.on_event(event)
    assert subject.health()["pending_captures"] == 1
    subject.on_event(FallEvent(camera_id="old", event_id="another-event"))
    assert subject.health()["last_error"] == "capture_capacity: RuntimeError"
    subject.clear_capture()
    subject.add_frame(b"new", 0)
    assert list(subject._ring) == [b"new"]
    assert subject.health()["pending_captures"] == 0


def test_constructor_bounds_existing_spool_after_restart(tmp_path):
    for index in range(3):
        path = tmp_path / f"event-{index}.mp4"
        path.write_bytes(b"123456")
        os.utime(path, (1000 + index, 1000 + index))
    subject = recorder(tmp_path, max_spool_bytes=10, max_spool_age_s=60, wall_clock=lambda: 1010)
    assert subject.health()["evicted_capacity"] == 2
    assert [path.name for path in tmp_path.glob("*.mp4")] == ["event-2.mp4"]
    assert subject.health()["spooled_bytes"] == 6
