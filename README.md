# mantau-agent

Mantau has two separate installable agent runtimes with one control-plane
contract:

- the Python agent for unattended Linux and 64-bit Raspberry Pi devices;
- the native Kotlin Android Agent in the separate `mantau-android-agent` repository for a spare phone that
  remains at home on the CCTV LAN.

Both use the shared enrollment, agent ID, capability, health, camera, command, and
inference-mode wire models from `mantau-core`. Android reports platform
`android`. The Android Agent is not `mantau-app`; RTSP, ONVIF, and monitoring
remain outside the Flutter control app.

The Linux/Pi agent discovers or accepts a manual RTSP camera, captures the
preferred low-bitrate stream, uploads sampled frames for server inference,
durably uploads health, and runs under systemd.
Raspberry Pi uses the same Linux ARM64 build and code path.

```text
ONVIF/manual setup -> RTSP validation -> durable config
    -> CameraPuller (bounded reconnect backoff + jitter, latest frame)
    -> bounded independent sampling queues
       CLOUD  -> sampled frame -> server-inference interface
       live   -> existing signed live-view frame endpoint
    -> SQLite event/heartbeat spool -> prompt retry after connectivity returns
    -> heartbeat + atomic local status snapshot
```

The Linux/Pi implementation reuses the existing `CameraPuller`, `FrameSampler`, core
detector protocol and adapters, `FrameUplink`, signed envelopes, sequence
counter, SQLite spool, and heartbeat contract. The Android implementation uses
the same v1 control payloads, signed live-frame protocol, envelope signatures,
and golden fixtures. Both running agents use CLOUD inference only. They do not
initialize local fall models or emit local detection events. The server runs
the fall and activity rules on signed sampled frames. Existing EDGE/HYBRID
values in stored configurations remain readable and resolve to CLOUD.
If server inference is unavailable, both agents keep control/configuration
available and report degraded monitoring. Capability probing and fresh-frame
retries recover cloud inference without reenrollment.

### Activity rules

Besides falls, the server runs mantau-core's activity rules on the same pose
observations, without extra models: prolonged position (on the floor, or
anywhere outside a seating/bed zone), nocturnal movement (repeated bed exits
or time out of bed inside the night window) and bathroom duration (someone
entered the bathroom-door zone and has not been seen since). Each raises a
warning and later a critical event with its own `kind`, except floor stillness
which emits one critical event at its saved threshold (30 seconds for new/reset
settings). Events are deduplicated by a
stable event id; signals carry only durations, movement, counts and
confidence, never images or identities. Timers pause while the camera is
disconnected, the person is lost or confidence is low. Thresholds, the night
window and zones come from the camera's detection settings: the saved copy is
applied by the server to the frames it analyses.

## Linux / Raspberry Pi installation

Build or obtain the binary matching the target:

- `mantau-agent-linux-x64` for Linux x86_64.
- `mantau-agent-linux-arm64` for Linux ARM64, including 64-bit Raspberry Pi OS.

Copy the binary and the `packaging/` directory to the device once, then run:

```sh
sudo sh packaging/install.sh ./mantau-agent-linux-arm64
```

The installer creates an unprivileged `mantau-agent` service account and safe
configuration/data directories, then launches one interactive setup. The
installed systemd unit uses `Restart=always`, so remote restart/reconfigure
commands can exit cleanly and be relaunched (an explicit `systemctl stop` is
still respected):

1. Enter the Mantau server URL, the **enrollment key** from the Mantau app
   (Beranda > Tambah perangkat; single use, valid for an hour), and a device
   name. The agent joins that household under a generated id and stores its
   own secret; nobody types or sees the secret. Enrollment is persisted
   immediately, so an interrupted camera step does not enroll again.
2. Normal installation uses `setup --remote`: finish camera discovery/manual
   entry, credentials and validation in the family app. Credentials are entered
   once and delivered to the agent in an authenticated durable command.
   The following local steps describe advanced standalone `setup` only.
   ONVIF discovery deduplicates devices by host and probes each one over RTSP.
   A single result is offered directly. Multiple results always require an
   explicit numbered choice; Enter never silently selects the first camera.
3. Use the manual address option for cameras without ONVIF or when multicast is
   blocked. Enter RTSP port, main path, optional low-bitrate/substream path, and
   credentials. Password input is hidden.
4. Setup opens the selected RTSP stream and decodes one frame before saving it.
   When a substream is configured, setup validates and persists it as the
   preferred runtime profile.
5. Server inference (`CLOUD`) is selected. The service starts and is enabled
   for future boots.

After installation and configuration, routine operation requires no SSH or
interactive login. systemd starts the agent at boot and restarts it after a
failure; camera and frame-upload outages are retried internally. If server
inference is unavailable at startup, the agent stays degraded and probes recovery. Configuration,
sequence state, acknowledged spool state, and health survive service restarts.

Useful local service commands are:

```sh
sudo systemctl status mantau-agent
sudo journalctl -u mantau-agent -f
sudo -u mantau-agent /usr/local/bin/mantau-agent \
  --config /etc/mantau-agent/config.json \
  --status-path /var/lib/mantau-agent/status.json status --json
sudo -u mantau-agent /usr/local/bin/mantau-agent \
  --config /etc/mantau-agent/config.json discover --json
```

Re-running `install.sh` updates the binary/unit without discarding state. Normal
uninstall preserves configuration and pending events:

```sh
sudo sh packaging/uninstall.sh
```

Only `packaging/uninstall.sh --purge` removes `/etc/mantau-agent`,
`/var/lib/mantau-agent`, and the service account.

## Android Agent

The native Android Agent lives in its own repository, `mantau-android-agent`,
with its own build, tests, and APK releases. Installation, permissions,
security, and RTSP details are in that repository's README.

## Camera setup behavior

`discover --json` returns a deduplicated inventory with host, ONVIF metadata,
RTSP reachability, and classified failure. It never returns camera credentials.
Discovery only locates a host; the configured RTSP paths remain authoritative
because ONVIF Media-service `GetProfiles`/`GetStreamUri` negotiation is not
implemented.

The setup wizard performs both the lightweight RTSP reachability probe and a
real one-frame OpenCV/FFmpeg read. The frame read is authoritative for cameras
whose RTSP server rejects unauthenticated `OPTIONS` but accepts credentials on
the media stream. A configuration is never promoted to `complete` without a
decoded frame. At runtime, a persisted host is used directly. Environment-only
deployments may enable ONVIF discovery; one reachable camera is accepted,
multiple reachable cameras fail with an instruction to run setup, and no result
falls back to `MANTAU_CAMERA_HOST` when supplied.

## Durable configuration and compatibility

The installed service uses:

| Path | Ownership/mode | Contents |
|---|---|---|
| `/etc/mantau-agent/config.json` | `mantau-agent`, `0600` inside a `0700` directory | Versioned enrollment, selected camera, credentials, inference mode, setup state |
| `/etc/mantau-agent/config.json.bak` | same | Previous validated configuration for explicit rollback |
| `/var/lib/mantau-agent/seq.txt` | private service state | Monotonic envelope sequence, atomically replaced before use |
| `/var/lib/mantau-agent/spool.db` | private service state | Unacknowledged signed events and heartbeats |
| `/var/lib/mantau-agent/status.json` | secret-free snapshot | Local health for `status --json` |
| `/var/lib/mantau-agent/commands.json` | private service state | Final results plus encrypted in-flight command custody for restart recovery |

Configuration and sequence writes use write/fsync/atomic-replace. Configuration
has a strict schema and unknown, truncated, or invalid data fails closed. A
corrupt sequence file also fails closed instead of restarting from zero and
reusing an acknowledged sequence number. To explicitly restore the prior
configuration:

```sh
sudo systemctl stop mantau-agent
sudo -u mantau-agent /usr/local/bin/mantau-agent \
  --config /etc/mantau-agent/config.json setup --restore-backup
sudo systemctl start mantau-agent
```

The agent does not log agent secrets or camera passwords. Its status and
discovery JSON contain no credentials. Linux directory/file permissions are
the confidentiality boundary; protect device administrator access and backups.

Unattended enrollment, without prompts:

```sh
mantau-agent --server-url https://server.example setup --remote \
  --key MTU-XXXXX-XXXXX-XXXXX-XXXXX --name "Ruang tamu"
```

For Docker, set `MANTAU_ENROLLMENT_KEY` (plus `MANTAU_SERVER_URL`, optionally
`MANTAU_DEVICE_NAME`): the first `run` with no saved enrollment enrolls once
and writes `config.json`; camera setup then continues from the app. Explicit
environment values override durable state, and a process whose camera is
configured by environment does not need camera setup from the app:

```sh
MANTAU_SERVER_URL=http://server:8100 \
MANTAU_ENROLLMENT_KEY=MTU-XXXXX-XXXXX-XXXXX-XXXXX \
MANTAU_CAMERA_ID=cam-1 \
MANTAU_CAMERA_HOST=192.168.1.42 \
MANTAU_CAMERA_SUB_PATH=/stream2 \
MANTAU_DEFAULT_STREAM_PROFILE=sub \
MANTAU_INFERENCE_MODE=CLOUD \
python -m mantau_agent.main run
```

The principal runtime variables are:

| Variable | Default | Purpose |
|---|---|---|
| `MANTAU_CONFIG_PATH` | `data/config.json` | Durable setup state path used by CLI/default run |
| `MANTAU_SERVER_URL` | `http://localhost:8100` | Server base URL |
| `MANTAU_ENROLLMENT_KEY`, `MANTAU_DEVICE_NAME` | empty | One-time enrollment for unattended installs |
| `MANTAU_AGENT_ID`, `MANTAU_AGENT_SECRET` | from `config.json` | Saved by enrollment; override only to reuse an existing identity |
| `MANTAU_CAMERA_HOST`, `MANTAU_CAMERA_PORT` | empty, `554` | Manual/discovery fallback |
| `MANTAU_CAMERA_MAIN_PATH`, `MANTAU_CAMERA_SUB_PATH` | `/stream1`, unset | RTSP profiles |
| `MANTAU_DEFAULT_STREAM_PROFILE` | `sub` | Preferred profile; core falls back to main if no sub path exists |
| `MANTAU_INFERENCE_MODE` | `CLOUD` | Existing saved values are treated as CLOUD at runtime |
| `MANTAU_DETECTOR_BACKEND` | `null` | Retained for old configs; no local model is loaded |
| `MANTAU_MODEL_DIR` | packaged | Retained for old configs; ignored by this runtime |
| `MANTAU_FALL_CLASSIFIER_ENABLED` | `true` | Retained for old configs; ignored by this runtime |
| `MANTAU_DETECTION_FPS` | `15` | Retained for old configs; local detection is disabled |
| `MANTAU_CLOUD_UPLOAD_FPS` | `10` | Frames per second uploaded for server inference (capped by the server's `max_fps`; below ~10 fps the fall tracker loses people mid-fall) |
| `MANTAU_CLOUD_INFERENCE_ENABLED` | `true` | Must remain enabled; server inference is required |
| `MANTAU_HYBRID_CONFIRMATION_FPS` | `0.2` | Retained for old configs; ignored by this runtime |
| `MANTAU_LIVE_VIEW_FPS` | `4` | Independent live-view rate |
| `MANTAU_FRAME_QUEUE_SIZE` | `2` | Pending frames per route; oldest drops first |
| `MANTAU_SEQ_PATH`, `MANTAU_SPOOL_PATH` | under `data/` | Durable uplink state |
| `MANTAU_SPOOL_RETRY_BASE_S`, `MANTAU_SPOOL_RETRY_CAP_S` | `0.5`, `15` | Full-jitter exponential server retry bounds |
| `MANTAU_STATUS_PATH`, `MANTAU_STATUS_INTERVAL_S` | `data/status.json`, `5` | Local status snapshot |
| `MANTAU_HEARTBEAT_INTERVAL_S` | `30` | Signed server health interval |
| `MANTAU_COMMAND_CHANNEL_ENABLED` | `false` | Opt into the additive authenticated command poller after the server is ready |
| `MANTAU_COMMAND_POLL_INTERVAL_S` | `5` | Poll delay; polling runs in its own asyncio task and never blocks capture/inference |
| `MANTAU_COMMAND_STATE_PATH` | `data/commands.json` | Durable completed-command ledger |

## Remote control-plane flow

When `MANTAU_COMMAND_CHANNEL_ENABLED=true`, the agent authenticates with the
same enrolled id/secret used by signed ingest. A separate task reports
secret-free capability/health metadata, polls for commands, acknowledges
`running`, executes locally, persists the final structured result, and then
uploads it. In-flight commands are encrypted locally with a key derived from
the enrolled agent secret before the server is allowed to erase its encrypted
credential blob. Persist-before-upload means a response lost during restart is
resent without repeating a completed discovery, camera operation, mode change,
restart, or reconfigure command.

Discovery reuses the ONVIF/RTSP implementation; camera tests reuse one-frame
validation; configuration is atomically saved to the existing versioned config;
mode changes use the live router and durable config; restart/reconfigure exits
cleanly so systemd applies the change. Credential-bearing command payloads are
never logged. They exist only in memory while executing and in the private
durable camera configuration after an accepted configuration command.

Compatibility/rollback: polling is off by default, so an upgraded agent behaves
like the old version until the server control plane is enabled. During mixed
rollout, old agents continue ingest/heartbeats and never consume queued work.
To roll back, disable command polling and restart the service; event/frame
uplinks and existing configuration are unchanged. The server may retain command
history and additive tables without affecting the old agent.

## Server inference boundary

`CLOUD` uploads sampled frames to `POST /agents/{id}/inference`; the server
runs fall and activity detection, stores events, and sends alerts. The agent
advertises only `AUTO` and `CLOUD` as compatible modes, recommends `CLOUD`,
and treats old saved mode choices as `CLOUD`. Control commands requesting EDGE
or HYBRID are refused. No local fall model loads. `NullDetector` remains in
source for isolated wiring tests but is not constructed by the runtime.

Server inference uses its own signed endpoint (`mantau_core.contracts.inference`),
never the live-view frame endpoint. `HttpInferenceUplink` reads the server's
capability at startup; startup fails when the server cannot offer inference.
Each frame is signed over
every header and its bytes, refused locally above the server's size limit,
retried at most once on a network/5xx error with the same frame id (the server
answers a retry without re-running the detector), dropped once older than the
server's `max_frame_age_s`, and never spooled.

Latency budget (CLOUD): the fall rules confirm a fall after the person has been
on the ground for 1 s, so landing-to-alert is that second plus the time from
capturing the confirming frame to the push request. Measured on a LAN with the
simulated camera (17 falls, the real pose detector on the server): capture to push
request median 87 ms, p95 99 ms, max 109 ms, i.e. about 1.1 s from landing.
Budget: 2 s from landing to push request on the local network, leaving the rest
of the 5 s target for WAN upload and FCM delivery. Sequential uploads reached
about 6.5 fps in that run (each waits for the server's answer).

## Resilience and health

Camera opens/reads use bounded native timeouts and reconnect forever through
the core full-jitter exponential backoff. HTTP connections are reused by
`httpx`; failed events and heartbeats enter the SQLite spool. A dedicated retry
worker begins promptly when the spool becomes non-empty, uses independently
bounded full-jitter backoff while offline, and drains oldest-first as soon as a
request succeeds. A drain lock prevents concurrent recovery workers from
replaying the same pending envelope. Server dedupe remains the final idempotency
boundary. Successful acknowledgements delete spool entries durably; the
monotonic sequence counter prevents reuse across clean restarts.

`status --json` reads the atomic, secret-free snapshot without starting camera
capture. It reports:

- camera connectivity, last frame time, last error, and reconnect count;
- requested and effective inference mode (always CLOUD) and whether server
  inference is available;
- uplink connectivity, last error, and last successful server contact;
- spool depth, queue/drop/upload details, version, PID, start time, and uptime.

If the recorded PID no longer exists, the command marks the snapshot stopped
and clears camera/uplink connectivity rather than reporting stale liveness.

Shutdown stops admission, drains active uploads, interrupts retry
waits, releases RTSP/native resources, writes a stopped status snapshot, closes
HTTP and SQLite, and lowers the tunnel. Pending envelopes and acknowledged
state remain durable for the next systemd start.

## Prevent desktop Linux sleep

An unattended laptop or desktop must remain awake when its lid/display policy
would otherwise suspend it. On a dedicated appliance, disable system sleep:

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

On a laptop, also configure `/etc/systemd/logind.conf` as appropriate for the
site, for example `HandleLidSwitch=ignore`, then restart `systemd-logind` or
reboot. This changes host-wide power behavior; coordinate it with the device
owner and ensure ventilation/power are suitable. Display blanking may remain
enabled because the agent is headless.

## Development and verification

Requires Python 3.10-3.12 and the sibling `../mantau-core` checkout.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests -q
```

The suite uses local sockets, mock HTTP, injected captures and inference
uplinks, and real SQLite files. It covers discovery deduplication and ambiguity,
RTSP validation, configuration interruption/corruption/rollback, environment
precedence, queue/rate behavior, CLOUD routing, prompt outage recovery, durable
acknowledgements, JSON status/discovery, shutdown, and packaging invariants.

Hardware ONVIF multicast, real camera credential variants, sustained upload
throughput to the server, Tailscale, systemd execution on an actual Linux
host, and cross-built binaries require target/integration verification. Build
and packaging commands are documented in `packaging/README.md`.
