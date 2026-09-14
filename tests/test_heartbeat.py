import asyncio

from mantau_agent.health.heartbeat import HeartbeatLoop


class _FakeUplink:
    def __init__(self) -> None:
        self.heartbeats = []

    async def send_heartbeat(self, heartbeat) -> None:
        self.heartbeats.append(heartbeat)


async def test_sends_a_heartbeat_immediately_then_on_each_interval():
    uplink = _FakeUplink()
    loop = HeartbeatLoop("agent-1", "cam-1", uplink, interval_s=0.02)
    stop_event = asyncio.Event()

    task = asyncio.create_task(loop.run(stop_event=stop_event))
    await asyncio.sleep(0.09)  # first send + a couple of interval ticks
    stop_event.set()
    await task

    assert len(uplink.heartbeats) >= 2
    assert all(h.agent_id == "agent-1" for h in uplink.heartbeats)
    assert all(h.camera_id == "cam-1" for h in uplink.heartbeats)


async def test_reports_live_status_via_injected_callables():
    uplink = _FakeUplink()
    reachable = {"value": True}
    depth = {"value": 0}

    loop = HeartbeatLoop(
        "agent-1", "cam-1", uplink, interval_s=0.02,
        camera_reachable=lambda: reachable["value"],
        queue_depth=lambda: depth["value"],
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(loop.run(stop_event=stop_event))
    await asyncio.sleep(0.01)

    reachable["value"] = False
    depth["value"] = 7
    await asyncio.sleep(0.03)
    stop_event.set()
    await task

    assert uplink.heartbeats[0].camera_reachable is True
    assert uplink.heartbeats[-1].camera_reachable is False
    assert uplink.heartbeats[-1].queue_depth == 7


async def test_stop_event_interrupts_the_wait_promptly():
    uplink = _FakeUplink()
    loop = HeartbeatLoop("agent-1", "cam-1", uplink, interval_s=10.0)  # long interval
    stop_event = asyncio.Event()

    task = asyncio.create_task(loop.run(stop_event=stop_event))
    await asyncio.sleep(0.01)  # let the first heartbeat send
    stop_event.set()

    await asyncio.wait_for(task, timeout=1.0)  # must not wait out the 10s interval
    assert len(uplink.heartbeats) == 1
