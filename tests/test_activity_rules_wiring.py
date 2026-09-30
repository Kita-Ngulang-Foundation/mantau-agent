"""The activity rules as the agent keeps them: registered, driven by saved
settings and paused across camera outages. The server runs them on the frames
it receives (inference is CLOUD only)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
from mantau_core.activity import (
    ActivityEngine, FrameObservation, PersonObservation, Posture, default_rules,
)
from mantau_core.contracts import DetectionSettings

from mantau_agent.activity_rules import build_activity_rules
from mantau_agent.pipeline import MonitoringPipeline

from test_inference_router import FRAME

T0 = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)  # 13:00 in Jakarta: daytime


def test_all_three_rules_are_registered():
    names = [type(rule).__name__ for rule in build_activity_rules()]
    assert names == ["ProlongedPositionRule", "NocturnalMovementRule", "BathroomDurationRule"]


class _Puller:
    def __init__(self):
        self.reachable = True
        self.frames = [(FRAME, 1)]

    def latest_frame(self):
        return self.frames[-1] if self.reachable else None


class _Router:
    def __init__(self):
        self.activity = ActivityEngine(default_rules())
        self.lost = 0
        self.activity.camera_lost = self._lost  # type: ignore[method-assign]

    def _lost(self):
        self.lost += 1

    def submit(self, image, ts):
        pass


async def test_camera_disconnect_pauses_activity_timers():
    puller, router = _Puller(), _Router()
    pipeline = MonitoringPipeline.__new__(MonitoringPipeline)
    pipeline.puller, pipeline.router, pipeline.poll_interval_s = puller, router, 0.001
    pipeline._stop = asyncio.Event()
    task = asyncio.create_task(pipeline._capture())
    await asyncio.sleep(0.02)
    puller.reachable = False
    await asyncio.sleep(0.02)
    puller.reachable = True
    await asyncio.sleep(0.02)
    puller.reachable = False
    await asyncio.sleep(0.02)
    pipeline._stop.set()
    await task
    assert router.lost == 2


def test_saved_settings_drive_the_rules():
    zones = [{"zone_id": "bed", "kind": "bed", "polygon": [
        {"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]}]
    settings = DetectionSettings(stillness={"floor_minutes": 0.5}, zones=zones)
    engine = ActivityEngine(build_activity_rules(), settings)
    person = PersonObservation(track_id=1, bbox=(0.4, 0.4, 0.1, 0.3), posture=Posture.LYING,
                               confidence=0.9)
    events = []
    for i in range(120):
        events += engine.update(FrameObservation(camera_id="cam", at=T0 + timedelta(seconds=i),
                                                 people=(person,)))
    assert events == []  # the whole frame is a bed: lying there is rest
    assert np.isclose(engine.settings.stillness.floor_minutes, 0.5)
