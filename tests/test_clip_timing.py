import asyncio
from datetime import datetime, timezone

from mantau_core.contracts import FallEvent
from mantau_agent.clips import ClipRecorder
import mantau_agent.clips as module


class LocalUploader:
    async def close(self): pass


def recorder(tmp_path, clock, **limits):
    return ClipRecorder(uploader=LocalUploader(), encode_jpeg=lambda t: str(t).encode(),
        spool_dir=tmp_path, fps=5, pre_s=15, post_s=15, retain_local=True,
        monotonic_clock=lambda: clock[0], **limits)


def candidate(name="fall"):
    return FallEvent(event_id=name, camera_id="camera", occurred_at=datetime.now(timezone.utc))


async def test_sparse_input_keeps_real_fifteen_second_windows(tmp_path, monkeypatch):
    encoded = []
    monkeypatch.setattr(module, "encode_mp4", lambda frames, fps: encoded.append((frames, fps)) or b"mp4")
    clock = [0.]
    subject = recorder(tmp_path, clock)
    for ts in range(0, 37500, 500):
        clock[0] = ts / 1000
        subject.add_frame(ts, ts)
    subject.on_event(candidate())
    for ts in range(37500, 52000, 500):
        clock[0] = ts / 1000
        subject.add_frame(ts, ts)
    clock[0] = 52
    subject.advance()  # No camera sample needs to land exactly on the deadline.
    await asyncio.gather(*tuple(subject._tasks))
    frames, fps = encoded[0]
    assert len(frames) == 150 and fps == 5
    assert int(frames[0]) == 22000 and int(frames[-1]) == 51500
    assert not subject._captures and (tmp_path / "fall.mp4").exists()
    await subject.close()


async def test_timer_saves_available_footage_when_no_frames_follow(tmp_path, monkeypatch):
    encoded = []
    monkeypatch.setattr(module, "encode_mp4", lambda frames, fps: encoded.append(frames) or b"mp4")
    clock = [0.]
    subject = recorder(tmp_path, clock)
    for ts in range(0, 15001, 200):
        clock[0] = ts / 1000
        subject.add_frame(ts, ts)
    subject.on_event(candidate())
    clock[0] = 29.9
    subject.advance()
    assert subject._captures and not encoded
    clock[0] = 30
    # Exercise the actual independent timer, without injecting another frame.
    await asyncio.sleep(.15)
    await asyncio.gather(*tuple(subject._tasks))
    assert (tmp_path / "fall.mp4").exists() and not subject._captures
    assert len(encoded[0]) == 76  # Available pre-event footage plus its last frame.
    await subject.close()


async def test_disable_cancels_pending_deadline_without_saving(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "encode_mp4", lambda *_: b"mp4")
    clock = [0.]
    subject = recorder(tmp_path, clock)
    subject.add_frame(0, 0)
    subject.on_event(candidate())
    subject.set_enabled(False)
    clock[0] = 20
    subject.advance()
    assert not list(tmp_path.glob("*.mp4")) and not subject._captures
    await subject.close()


async def test_due_clips_wait_for_bounded_encoder_instead_of_being_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "encode_mp4", lambda *_: b"mp4")
    clock = [0.]
    subject = recorder(tmp_path, clock, max_encoding_tasks=1)
    subject.add_frame(0, 0)
    for index in range(3): subject.on_event(candidate(f"fall-{index}"))
    clock[0] = 15
    for _ in range(3):
        subject.advance()
        await asyncio.gather(*tuple(subject._tasks))
    assert len(list(tmp_path.glob("*.mp4"))) == 3 and not subject._captures
    assert subject.failed == 0
    await subject.close()
