"""The "agent emits" half of the wire contract: `Envelope.for_event()` /
`for_heartbeat()`, built from the same field values as the golden fixtures
in `../../protocol/examples/`, must produce a byte-identical envelope --
same fields, same signature -- not just "roughly the same shape."

Only needs `mantau_core` (already a dependency of this package); nothing
agent-specific is under test here, which is the point: this proves the
CONTRACT mantau_agent.uplink.client.UplinkClient builds on top of, not
UplinkClient's own code (see test_uplink_client.py for that).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from mantau_core.contracts import Envelope, EventKind, FallEvent, Heartbeat, Severity

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "protocol" / "examples"
TEST_SECRET = "golden-example-shared-secret-do-not-use"  # see examples/README.md
FIXED_TIME = datetime(2026, 9, 13, 4, 12, 3, 114000, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def test_fall_event_envelope_matches_the_golden_fixture_byte_for_byte():
    golden = _load("fall_event_envelope.json")

    event = FallEvent(
        event_id="ev-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        camera_id="cam-kamar-ibu",
        kind=EventKind.FALL,
        severity=Severity.CRITICAL,
        occurred_at=FIXED_TIME,
        confidence=0.91,
        track_id=3,
        signals={"velocity": 0.52, "torso_angle_deg": 84.0},
    )
    envelope = Envelope.for_event("agent-3f9a1c2b", seq=42, event=event)
    envelope = envelope.model_copy(update={"sent_at": FIXED_TIME}).sign(TEST_SECRET)

    assert envelope.model_dump(mode="json") == golden


def test_heartbeat_envelope_matches_the_golden_fixture_byte_for_byte():
    golden = _load("heartbeat_envelope.json")

    heartbeat = Heartbeat(
        agent_id="agent-3f9a1c2b", camera_id="cam-kamar-ibu", sent_at=FIXED_TIME,
        camera_reachable=True, detector_alive=True, queue_depth=0,
    )
    envelope = Envelope.for_heartbeat("agent-3f9a1c2b", seq=43, heartbeat=heartbeat)
    envelope = envelope.model_copy(update={"sent_at": FIXED_TIME}).sign(TEST_SECRET)

    assert envelope.model_dump(mode="json") == golden


def test_golden_fixtures_verify_against_the_documented_test_secret():
    """A parity check on the fixtures themselves -- if someone edits
    protocol/examples/*.json by hand later, this catches a `sig` that no
    longer matches its own content before the two tests above do the more
    specific field-by-field comparison."""
    for name in ("fall_event_envelope.json", "heartbeat_envelope.json"):
        envelope = Envelope.model_validate(_load(name))
        assert envelope.verify(TEST_SECRET) is True
