# mantau-agent

Mantau has two separate installable agent runtimes with one control-plane
contract:

- the Python agent for unattended Linux and 64-bit Raspberry Pi devices;
- the native Kotlin Android Agent under `android-agent/` for a spare phone that
  remains at home on the CCTV LAN.

Both use the shared claim, agent ID, capability, health, camera, command, and
inference-mode wire models from `mantau-core`. Android reports platform
`android`. The Android Agent is not `mantau-app`; RTSP, ONVIF, and monitoring
remain outside the Flutter control app.

The Linux/Pi agent discovers or accepts a manual RTSP camera, captures the
preferred low-bitrate stream, routes sampled frames through the configured
inference mode, durably uploads events and health, and runs under systemd.
Raspberry Pi uses the same Linux ARM64 build and code path.

```text
ONVIF/manual setup -> RTSP validation -> durable config
    -> CameraPuller (bounded reconnect backoff + jitter, latest frame)
    -> bounded independent sampling queues
       EDGE   -> local detector -> signed event uplink
       CLOUD  -> sampled frame -> server-inference interface
       HYBRID -> local event + rate-limited server confirmation
       live   -> existing signed live-view frame endpoint
    -> SQLite event/heartbeat spool -> prompt retry after connectivity returns
    -> heartbeat + atomic local status snapshot
```

The Linux/Pi implementation reuses the existing `CameraPuller`, `FrameSampler`, core
detector protocol and adapters, `FrameUplink`, signed envelopes, sequence
counter, SQLite spool, and heartbeat contract. The Android implementation uses
the same v1 control payloads, signed live-frame protocol, envelope signatures,
and golden fixtures. Both agents detect falls on device (EDGE): the Python agent
through `mantau_core.detection.MediapipeDetector`, the Android agent through
MediaPipe Tasks Pose Landmarker plus a Kotlin port of the same fall rules and the
same ONNX classifier. Model files are SHA-256 verified against mantau-core's
pinned manifest before loading, and shared pose-sequence fixtures hold both ports
to identical fall decisions. CLOUD and HYBRID stay unavailable (no server
inference endpoint exists) and are never advertised as supported.

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

1. Enter the Mantau server URL and device name. Enrollment is persisted
   immediately, so an interrupted camera step does not enroll the same agent
   again.
2. ONVIF discovery deduplicates devices by host and probes each one over RTSP.
   A single result is offered directly. Multiple results always require an
   explicit numbered choice; Enter never silently selects the first camera.
3. Use the manual address option for cameras without ONVIF or when multicast is
   blocked. Enter RTSP port, main path, optional low-bitrate/substream path, and
   credentials. Password input is hidden.
4. Setup opens the selected RTSP stream and decodes one frame before saving it.
   When a substream is configured, setup validates and persists it as the
   preferred runtime profile.
5. Choose `AUTO`, `EDGE`, `CLOUD`, or `HYBRID`. The service starts and is enabled
   for future boots.

After installation and configuration, routine operation requires no SSH or
interactive login. systemd starts the agent at boot and restarts it after a
failure; camera and server outages are retried internally. Configuration,
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

## Android Agent installation

Build the independent native project with Android SDK 36 and JDK 17 or 21:

```powershell
cd android-agent
.\gradlew.bat testDebugUnitTest assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Open **Mantau Agent** on the spare Android 8.0+ phone, enter the server URL and
device name, enroll, then copy its claim code into `mantau-app`. Discover ONVIF
cameras only while the phone is connected to the CCTV Wi-Fi, or use manual
IP/RTSP configuration. Select a substream path when available, save, grant the
notification permission, and start the foreground monitoring service.

Android identity/configuration is stored in private app storage; agent/camera
secrets and in-flight credential-bearing commands are encrypted with an Android
Keystore AES-GCM key. The persistent notification reports failure/degraded
state without credentials. See `android-agent/README.md` for Android build,
installation, permission, security, RTSP, and hardware-test details.

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

Existing `MANTAU_*` environment variables and `.env` files remain supported for
Docker and CI. Explicit environment values override durable state. A fully
environment-configured process does not require `config.json`:

```sh
MANTAU_SERVER_URL=http://server:8100 \
MANTAU_AGENT_ID=agent-1 \
MANTAU_AGENT_SECRET='<secret>' \
MANTAU_CAMERA_ID=cam-1 \
MANTAU_CAMERA_HOST=192.168.1.42 \
MANTAU_CAMERA_SUB_PATH=/stream2 \
MANTAU_DEFAULT_STREAM_PROFILE=sub \
MANTAU_INFERENCE_MODE=AUTO \
python -m mantau_agent.main run
```

The principal runtime variables are:

| Variable | Default | Purpose |
|---|---|---|
| `MANTAU_CONFIG_PATH` | `data/config.json` | Durable setup state path used by CLI/default run |
| `MANTAU_SERVER_URL` | `http://localhost:8100` | Server base URL |
| `MANTAU_AGENT_ID`, `MANTAU_AGENT_SECRET` | empty | Enrollment identity |
| `MANTAU_CAMERA_HOST`, `MANTAU_CAMERA_PORT` | empty, `554` | Manual/discovery fallback |
| `MANTAU_CAMERA_MAIN_PATH`, `MANTAU_CAMERA_SUB_PATH` | `/stream1`, unset | RTSP profiles |
| `MANTAU_DEFAULT_STREAM_PROFILE` | `sub` | Preferred profile; core falls back to main if no sub path exists |
| `MANTAU_INFERENCE_MODE` | `AUTO` | Requested mode |
| `MANTAU_DETECTOR_BACKEND` | `null` | `null` or `mediapipe` (real fall detection; needs `mantau-core[detection]`) |
| `MANTAU_MODEL_DIR` | packaged | Directory with the pinned model files; each is SHA-256 verified before loading |
| `MANTAU_FALL_CLASSIFIER_ENABLED` | `true` | Learned confirmation layer on top of the fall rules |
| `MANTAU_DETECTION_FPS` | `15` | Local sample cap and AUTO throughput target (below ~15 fps the fall tracker loses people mid-fall) |
| `MANTAU_CLOUD_UPLOAD_FPS` | `1` | Cloud sample/attempt cap |
| `MANTAU_HYBRID_CONFIRMATION_FPS` | `0.2` | HYBRID confirmation cap |
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

## Inference modes and server boundary

| Mode | Effective behavior |
|---|---|
| `EDGE` | Run the local detector and send real events only. |
| `CLOUD` | Upload sampled frames through `InferenceUplink`; skip local detection. |
| `HYBRID` | Send local events immediately and submit explicitly rate-limited event frames for server confirmation. |
| `AUTO` | Select EDGE when a production detector initializes and its benchmark (real pose + rules + classifier on a frame with a person) succeeds. If throughput or memory is below budget, CLOUD is chosen only when a cloud transport exists; otherwise EDGE still runs, with the reason. |

Capability reports list only modes that can run: EDGE when the detector loaded and
benchmarked, CLOUD only with an injected cloud transport, HYBRID only with both.
With neither, the recommended mode is `AUTO` and the reason says why.

`NullDetector` remains compatible for wiring tests, but the production router
suppresses all its output and any event marked `signals.synthetic`. Synthetic
detections never reach alert or confirmation uplinks.

The server has `/ingest` for signed events/heartbeats, a live-view frame
endpoint, and the optional authenticated control plane above. It still does not
expose a server-inference endpoint. CLOUD/HYBRID
inference is therefore isolated behind `InferenceUplink`; without an injected
adapter, status reports `cloud_available=false` and `degraded=true`. The agent
does not invent an endpoint or treat live-view storage as inference. The
remaining server/core work is an authenticated inference submission contract,
frame/event correlation and confirmation results, plus a remote capability and
detailed-health contract.

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
- effective inference mode and detector backend/liveness;
- uplink connectivity, last error, and last successful server contact;
- spool depth, queue/drop/upload details, version, PID, start time, and uptime.

If the recorded PID no longer exists, the command marks the snapshot stopped
and clears camera/uplink connectivity rather than reporting stale liveness.

Shutdown stops admission, drains active detector/event work, interrupts retry
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

The suite uses local sockets, mock HTTP, injected captures/detectors, and real
SQLite files. It covers discovery deduplication and ambiguity, RTSP validation,
configuration interruption/corruption/rollback, environment precedence,
queue/rate behavior, every inference mode, prompt outage recovery, durable
acknowledgements, JSON status/discovery, shutdown, and packaging invariants.

Hardware ONVIF multicast, real camera credential variants, MediaPipe model
accuracy/sustained throughput, Tailscale, systemd execution on an actual Linux
host, and cross-built binaries require target/integration verification. Build
and packaging commands are documented in `packaging/README.md`.
