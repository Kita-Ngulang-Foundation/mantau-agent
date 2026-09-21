import asyncio

import httpx
import numpy as np
from mantau_core.buffer import DurableSpool
from mantau_core.contracts import FallEvent
from mantau_core.resilience import BackoffPolicy

from mantau_agent.capabilities import CapabilityReport, InferenceMode, PlatformType
from mantau_agent.detect.router import InferenceRouter
from mantau_agent.health.status import StatusStore
from mantau_agent.pipeline import MonitoringPipeline
from mantau_agent.uplink.client import UplinkClient
from mantau_agent.uplink.frames import FrameUplink
from mantau_agent.uplink.seq import CorruptSequenceError, SeqCounter
from mantau_agent.uplink.spool import EnvelopeSpool
from mantau_agent.uplink.tunnel import NullTunnel


class Puller:
    reachable = True
    last_frame_at = None
    last_error = None
    restarts = 0

    def start(self): pass
    def stop(self): self.reachable = False
    def latest_frame(self): return None


async def test_spool_drains_promptly_after_server_recovery_and_stays_acked(tmp_path):
    online = {"value": False}
    accepted = []

    def handler(request):
        if not online["value"]:
            return httpx.Response(503)
        accepted.append(request)
        return httpx.Response(200)

    spool_path = tmp_path / "spool.db"
    seq_path = tmp_path / "seq.txt"
    durable = DurableSpool(spool_path, ttl_s=300)
    spool = EnvelopeSpool(durable)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        uplink = UplinkClient("http://server", "agent", "secret", SeqCounter(seq_path),
                              spool, client=http)
        frame_uplink = FrameUplink("http://server", "agent", "secret", "cam", client=http)
        capabilities = CapabilityReport(
            platform=PlatformType.LINUX_X86_64, architecture="x86_64", cpu="test",
            cpu_count=2, memory_bytes=1024**3, software_version="test",
            detector_backend="null", supported_detector_backends=["null"],
        )
        router = InferenceRouter(
            camera_id="cam", detector=None, event_uplink=uplink, frame_uplink=frame_uplink,
            capabilities=capabilities, mode=InferenceMode.CLOUD, live_view_enabled=False)
        pipeline = MonitoringPipeline(
            puller=Puller(), router=router, uplink=uplink, spool=spool,
            tunnel=NullTunnel(), agent_id="agent", camera_id="cam",
            heartbeat_interval_s=60,
            spool_backoff=BackoffPolicy(base=.001, cap=.01),
            status_store=StatusStore(tmp_path / "status.json"), status_interval_s=.01,
        )
        await pipeline.start()
        await uplink.send_event(FallEvent(camera_id="cam", event_id="event-one"))
        assert spool.depth() >= 1
        online["value"] = True
        for _ in range(100):
            if spool.depth() == 0:
                break
            await asyncio.sleep(.005)
        assert spool.depth() == 0
        assert uplink.server_reachable
        assert uplink.last_successful_contact is not None
        await pipeline.shutdown()

    with DurableSpool(spool_path) as reopened:
        assert EnvelopeSpool(reopened).pending() == []
    # A new process starts after the durable ack: nothing is replayed.
    with DurableSpool(spool_path) as reopened:
        replay = EnvelopeSpool(reopened)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            restarted = UplinkClient("http://server", "agent", "secret", SeqCounter(seq_path),
                                     replay, client=http)
            assert await restarted.drain_spool() == 0
    assert len([request for request in accepted if b"event-one" in request.content]) == 1


def test_corrupted_sequence_fails_closed_instead_of_reusing_acknowledged_ids(tmp_path):
    path = tmp_path / "seq.txt"
    path.write_text("not-a-number", encoding="utf-8")
    try:
        SeqCounter(path)
        assert False, "expected corruption to fail closed"
    except CorruptSequenceError as exc:
        assert "refusing to reuse" in str(exc)
