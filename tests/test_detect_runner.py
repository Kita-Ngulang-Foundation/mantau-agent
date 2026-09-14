import asyncio

import numpy as np
from mantau_core.detection import NullDetector

from mantau_agent.detect.runner import DetectRunner
from mantau_agent.detect.sampler import FrameSampler

FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


class _FakePuller:
    """Hands out a new (frame, ts_ms) pair every call, advancing by 33ms
    each time (~30fps)."""

    def __init__(self) -> None:
        self._ts = 0

    def latest_frame(self):
        self._ts += 33
        return (FRAME, self._ts)


async def test_runner_calls_on_event_when_the_detector_fires():
    events_seen = []

    async def on_event(event):
        events_seen.append(event)

    runner = DetectRunner(_FakePuller(), NullDetector("cam-1", trigger_every_n_frames=5),
                           on_event, poll_interval_s=0.001)
    stop_event = asyncio.Event()

    async def run_briefly():
        task = asyncio.create_task(runner.run(stop_event=stop_event))
        for _ in range(50):
            await asyncio.sleep(0.005)
            if len(events_seen) >= 2:
                break
        stop_event.set()
        await task

    await run_briefly()
    assert len(events_seen) >= 2
    assert all(e.camera_id == "cam-1" for e in events_seen)


async def test_runner_never_calls_on_event_when_the_detector_never_fires():
    events_seen = []

    async def on_event(event):
        events_seen.append(event)

    runner = DetectRunner(_FakePuller(), NullDetector("cam-1"), on_event, poll_interval_s=0.001)
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(stop_event=stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await task

    assert events_seen == []


async def test_runner_respects_the_sampler_and_skips_dropped_frames():
    seen_ts: list[int] = []

    class _RecordingDetector:
        def push(self, frame, ts_ms):
            seen_ts.append(ts_ms)
            return []

        def close(self):
            pass

    async def on_event(event):
        pass

    # keep_every_n=3 means only every third frame reaches the detector at all
    sampler = FrameSampler(keep_every_n=3)
    runner = DetectRunner(_FakePuller(), _RecordingDetector(), on_event,
                          sampler=sampler, poll_interval_s=0.001)
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(stop_event=stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await task

    assert len(seen_ts) > 0
    # every kept ts should be a multiple of the puller's 33ms step times 3
    assert all(ts % (33 * 3) == 0 for ts in seen_ts)
