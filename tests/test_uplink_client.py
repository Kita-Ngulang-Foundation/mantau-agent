"""UplinkClient against a mocked HTTP transport -- no real network, no real
server. Exercises the three paths that matter: a clean send never spools,
any failure spools instead of raising, and a later successful send drains
what piled up.
"""

from __future__ import annotations

import httpx
from mantau_core.buffer import DurableSpool
from mantau_core.contracts import Envelope, FallEvent, Heartbeat

from mantau_agent.uplink.client import UplinkClient
from mantau_agent.uplink.seq import SeqCounter
from mantau_agent.uplink.spool import EnvelopeSpool


def _client_and_spool(handler, tmp_path) -> tuple[UplinkClient, EnvelopeSpool]:
    seq = SeqCounter(tmp_path / "seq.txt")
    spool = EnvelopeSpool(DurableSpool(tmp_path / "spool.db"))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = UplinkClient("http://server.local:8100", "agent-1", "shared-secret", seq, spool,
                          client=http_client)
    return client, spool


async def test_send_event_succeeds_without_spooling(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "accepted", "duplicate": False, "out_of_order": False})

    client, spool = _client_and_spool(handler, tmp_path)
    await client.send_event(FallEvent(camera_id="cam-1"))

    assert len(calls) == 1
    assert spool.depth() == 0
    await client.close()


async def test_send_event_spools_on_server_error(tmp_path):
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    client, spool = _client_and_spool(handler, tmp_path)
    await client.send_event(FallEvent(camera_id="cam-1"))

    assert spool.depth() == 1
    await client.close()


async def test_send_event_spools_on_connection_failure(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client, spool = _client_and_spool(handler, tmp_path)
    await client.send_event(FallEvent(camera_id="cam-1"))

    assert spool.depth() == 1
    await client.close()


async def test_a_successful_send_drains_previously_spooled_envelopes(tmp_path):
    seq = SeqCounter(tmp_path / "seq.txt")
    spool = EnvelopeSpool(DurableSpool(tmp_path / "spool.db"))

    def failing(request):
        return httpx.Response(500)

    failing_client = UplinkClient("http://server.local", "agent-1", "secret", seq, spool,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(failing)))
    await failing_client.send_event(FallEvent(camera_id="cam-1"))
    assert spool.depth() == 1
    await failing_client.close()

    # a new client, same spool/seq -- as if the process restarted after an outage
    calls = []

    def succeeding(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "accepted", "duplicate": False, "out_of_order": False})

    recovered_client = UplinkClient("http://server.local", "agent-1", "secret", seq, spool,
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(succeeding)))
    await recovered_client.send_event(FallEvent(camera_id="cam-1"))  # triggers send + drain

    assert spool.depth() == 0     # both the new event and the spooled one landed
    assert len(calls) == 2
    await recovered_client.close()


async def test_drain_stops_at_the_first_failure_without_acking_anything(tmp_path):
    seq = SeqCounter(tmp_path / "seq.txt")
    spool = EnvelopeSpool(DurableSpool(tmp_path / "spool.db"))
    spool.put(Envelope.for_event("agent-1", seq=0, event=FallEvent(camera_id="cam-1")).sign("secret"))
    spool.put(Envelope.for_event("agent-1", seq=1, event=FallEvent(camera_id="cam-1")).sign("secret"))

    def always_fails(request):
        return httpx.Response(500)

    client = UplinkClient("http://server.local", "agent-1", "secret", seq, spool,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(always_fails)))

    sent = await client.drain_spool()
    assert sent == 0
    assert spool.depth() == 2  # the outage is presumably still ongoing -- nothing acked
    await client.close()


async def test_send_heartbeat_signs_and_sends_too(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "accepted", "duplicate": False, "out_of_order": False})

    client, _spool = _client_and_spool(handler, tmp_path)
    heartbeat = Heartbeat(agent_id="agent-1", camera_reachable=True, detector_alive=True)
    await client.send_heartbeat(heartbeat)

    assert len(calls) == 1
    await client.close()
