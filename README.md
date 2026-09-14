# mantau-agent

The agent half of **Scenario 2**. Runs on the customer's LAN, pulls RTSP
from the camera locally, and opens an OUTBOUND connection to the server --
never dials in, which is what survives CGNAT regardless of the ISP. See
`../protocol/PROTOCOL.md` for the wire contract, and `../README.md` for how
agent and server fit together.

For this weekend, this runs on a laptop or Docker container standing in for
the eventual dedicated device (target hardware: Orange Pi Zero 2W class,
~Rp350k, decided separately).

```
 camera (LAN) --pull--> CameraPuller --frames--> DetectRunner --events--> UplinkClient
                                                                              |  \
                                                                    on failure  on success
                                                                       spool         POST /ingest
                                                                    (SQLite,      (signed envelope)
                                                                    survives an
                                                                     outage)
```

## Install (local dev)

Requires Python 3.10â€“3.12, and `mantau-core` checked out two levels up
(`../../mantau-core`) -- it isn't published anywhere yet.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

Enroll against a running server first (see `../server/README.md`), then:

```powershell
$env:MANTAU_SERVER_URL = "http://localhost:8100"
$env:MANTAU_AGENT_ID = "agent-1"
$env:MANTAU_AGENT_SECRET = "<from POST /agents/enroll>"
$env:MANTAU_CAMERA_ID = "cam-1"
$env:MANTAU_CAMERA_HOST = "192.168.1.42"
.venv\Scripts\python.exe -m mantau_agent.main
```

With no camera reachable, `CameraPuller` just backs off and retries forever
-- fine for confirming the process starts and enrolls correctly before a
real camera is on the network.

## Configuration

Env vars, all prefixed `MANTAU_` (see `mantau_core.config.CoreSettings` for
the inherited ones -- `spool_ttl_s` in particular, the "hold recent events
through a brief outage" window, reused directly rather than redeclared):

| Var | Default | |
|---|---|---|
| `MANTAU_SERVER_URL` | `http://localhost:8100` | Where `/ingest` lives. |
| `MANTAU_AGENT_ID`, `MANTAU_AGENT_SECRET` | *(required)* | From the server's `POST /agents/enroll`. |
| `MANTAU_CAMERA_HOST`, `MANTAU_CAMERA_PORT`, `MANTAU_CAMERA_MAIN_PATH`, `MANTAU_CAMERA_SUB_PATH`, `MANTAU_CAMERA_USERNAME`, `MANTAU_CAMERA_PASSWORD` | -- | Manual camera config. ONVIF discovery (`discovery/onvif.py`) is built but not wired into `main.py` yet -- see "Known gaps". |
| `MANTAU_DETECTOR_BACKEND` | `null` | `null` (works today) or `mediapipe` (needs `mantau-core[detection]` **and** mantau-ai to ship a streaming entrypoint). |
| `MANTAU_NULL_DETECTOR_TRIGGER_EVERY` | `150` | With the null backend, fire a synthetic FallEvent every N frames -- proves capture â†’ relay â†’ server â†’ alert without real detection. |
| `MANTAU_SAMPLER_KEEP_EVERY_N`, `MANTAU_SAMPLER_MAX_FPS` | `1`, unset | Frame decimation -- see `detect/sampler.py`. The target hardware cannot run detection at full camera framerate. |
| `MANTAU_TUNNEL_PROVIDER` | `null` | `null` (direct reachability -- fine when agent and server share a network, e.g. in Docker Compose) or `tailscale` (shells out to the real CLI). |
| `MANTAU_SEQ_PATH`, `MANTAU_SPOOL_PATH` | `data/seq.txt`, `data/spool.db` | The crash-safe seq counter and the outage spool -- see `uplink/`. |
| `MANTAU_HEARTBEAT_INTERVAL_S` | `30.0` | |

## Test

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

62 tests. Real local sockets back the RTSP/ONVIF-probe tests; a real SQLite
spool backs the outage-recovery tests; `httpx.MockTransport` backs the
uplink client tests (no real network). `test_contract_envelopes.py` proves
this package's envelope construction matches `../protocol/examples/`
byte-for-byte -- the other half of that check lives in
`../server/tests/test_contract_envelopes.py`.

Real UDP multicast (`discovery.onvif.discover()`) and a real Tailscale
connection (`uplink.tunnel.TailscaleTunnel.up()`) are not exercised by the
suite -- both need hardware/accounts this environment doesn't have. Their
pure logic (SOAP message construction/parsing, the missing-binary error
path) is tested; the actual network calls are documented as untested in
each module's docstring, not silently assumed to work.

## Known gaps

- **ONVIF discovery isn't wired into `main.py`.** `discovery/onvif.py` is
  built and tested (message construction, ProbeMatch parsing), but
  `main.py` only builds a `CameraRef` from manual config
  (`MANTAU_CAMERA_HOST` etc.) -- `MANTAU_USE_ONVIF_DISCOVERY` exists in
  `config.py` but nothing reads it yet. Wiring it means: call `discover()`,
  take the first match's `host`, fall back to manual config if discovery
  finds nothing.
- **No ONVIF Media-service stream URI negotiation.** See
  `discovery/onvif.py`'s docstring -- WS-Discovery only finds a camera's
  device service; the formally-correct way to get its actual RTSP URI is a
  separate `GetProfiles`/`GetStreamUri` SOAP exchange, not implemented.
  `CameraRef.paths` (the `/stream1`/`/stream2` convention) is what's
  actually used once a camera is located.
- **Docker Compose is unverified end-to-end** (`../docker/compose.yaml`) --
  run it against a live Docker engine before a demo depends on it.
- **Spool recovery latency is bounded by the heartbeat interval, not
  instant.** `UplinkClient` drains the spool opportunistically after every
  successful send, and the heartbeat loop already provides a steady
  `MANTAU_HEARTBEAT_INTERVAL_S` (default 30s) cadence of attempts even when
  no fall events are firing -- so an outage does self-heal without a
  dedicated retry task. What's missing is a SHORTER, dedicated retry loop
  for when the spool is non-empty, so recovery isn't tied to the heartbeat
  interval specifically.
