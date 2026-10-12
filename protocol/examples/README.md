# Golden envelope examples

These test fixtures were generated with `mantau_core.contracts`. The Python
tests in `tests/test_contract_envelopes.py` and Android tests in
`android-agent/app/src/test/java/id/mantau/agent/uplink/GoldenProtocolTest.kt`
compare emitted envelopes and signatures against them.

Both fixtures use the test-only secret
`golden-example-shared-secret-do-not-use`. If the contract changes, regenerate
the fixtures with the fixed `sent_at` and `occurred_at` timestamp
`2026-09-13T04:12:03.114000Z` so changes remain visible in version control.

## Optional heartbeat fields

Heartbeats may carry two optional fields:

- `agent_version` (string): the agent software version.
- `stats` (object of numbers): performance counters. Allowed keys are exactly
  the names in `mantau_core.contracts.HEARTBEAT_STATS`. Counters are deltas
  since the previous heartbeat that was built, and values are rounded to one
  decimal place.

Both fields are omitted when unset (`agent_version` when it is None, `stats`
when it is empty), so `heartbeat_envelope.json` is unchanged.
`heartbeat_envelope_stats.json` shows a heartbeat that carries them. It is
generated with `mantau_core.contracts`, the same test-only secret and the same
fixed timestamp as the other fixtures.
