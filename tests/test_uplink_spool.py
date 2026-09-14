from mantau_core.buffer import DurableSpool
from mantau_core.contracts import Envelope, FallEvent

from mantau_agent.uplink.spool import EnvelopeSpool


def _envelope(seq: int) -> Envelope:
    return Envelope.for_event("agent-1", seq=seq, event=FallEvent(camera_id="cam-1"))


def test_put_and_pending_round_trips_full_envelopes(tmp_path):
    with DurableSpool(tmp_path / "spool.db") as durable:
        spool = EnvelopeSpool(durable)
        env = _envelope(0)
        spool.put(env)

        pending = spool.pending()
        assert len(pending) == 1
        assert pending[0].agent_id == "agent-1"
        assert pending[0].seq == 0
        assert pending[0].event().camera_id == "cam-1"


def test_ack_removes_the_envelope(tmp_path):
    with DurableSpool(tmp_path / "spool.db") as durable:
        spool = EnvelopeSpool(durable)
        env = _envelope(0)
        spool.put(env)
        spool.ack(env)
        assert spool.pending() == []
        assert spool.depth() == 0


def test_pending_returns_oldest_first(tmp_path):
    with DurableSpool(tmp_path / "spool.db") as durable:
        spool = EnvelopeSpool(durable)
        spool.put(_envelope(0))
        spool.put(_envelope(1))
        spool.put(_envelope(2))
        assert [e.seq for e in spool.pending()] == [0, 1, 2]


def test_survives_close_and_reopen(tmp_path):
    db_path = tmp_path / "spool.db"
    durable = DurableSpool(db_path)
    spool = EnvelopeSpool(durable)
    spool.put(_envelope(5))
    durable.close()

    reopened_durable = DurableSpool(db_path)
    try:
        reopened_spool = EnvelopeSpool(reopened_durable)
        assert [e.seq for e in reopened_spool.pending()] == [5]
    finally:
        reopened_durable.close()
