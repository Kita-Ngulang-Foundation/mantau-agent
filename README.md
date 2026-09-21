# mantau-agent

LAN camera monitoring with outbound server connections. The pipeline composes
existing discovery, RTSP capture, samplers, detector adapters, JPEG upload,
signed event envelopes, SQLite spool, and heartbeat components.

```text
manual / ONVIF discovery -> CameraPuller (RTSP, latest frame)
  -> independent FrameSamplers -> InferenceRouter
       EDGE   -> Detector -> UplinkClient -> /ingest
       CLOUD  -> FrameUplink.encode -> InferenceUplink (integration interface)
       HYBRID -> Detector -> events + rate-limited confirmation frames
       live   -> FrameUplink -> /cameras/{camera_id}/frame (CLOUD/HYBRID only)
  event / heartbeat upload failure -> EnvelopeSpool -> DurableSpool (SQLite)
  health -> existing signed heartbeat + detailed local status/logs
```

`DetectRunner`, `FrameUplink.run`, and the core detector protocol remain
available for existing callers. The main process uses `MonitoringPipeline`
and `InferenceRouter` to give each frame path a separate bounded queue.

## Platforms and inference modes

Platform types identify Linux x86_64, Linux ARM64, Raspberry Pi, and Android.
Raspberry Pi is identified from the device-tree model; its actual architecture
is reported separately. Other hosts (including Windows development) report
`other`. **Android is identification only: no Android build, APK, service,
or tested Termux deployment is provided by this change.**

| Mode | Behavior |
|---|---|
| `EDGE` | Local detector; uplink events and health only. Live-view frames are suppressed even if live view is enabled. |
| `CLOUD` | Sample and upload frames through `InferenceUplink`; never call the local detector. Live view has a separate rate and transport. |
| `HYBRID` | Local events are sent immediately. Frames associated with real events may be submitted for server confirmation, capped by both the cloud-upload and confirmation rates. Confirmation does not delay or retract an event. |
| `AUTO` | Deterministically choose EDGE or CLOUD from the capability report. HYBRID requires explicit selection. |

AUTO selects EDGE only when the configured production backend successfully
initializes and benchmarks at least `detection_fps`, and measured host memory
is at least 512 MiB. Missing/failed detectors, unknown measurements, low memory,
or insufficient throughput select CLOUD with an explicit reason. NullDetector
never qualifies as production inference. This policy does not assume that an
ARM board is weak or that an accelerator makes a detector fast.

`CapabilityReport.model_dump_json()` serializes platform, architecture, CPU,
CPU count, host memory, detected accelerators, usable detector backends,
software version, detector throughput/error, and recommended mode. The probe
warms a disposable detector, measures three 320x240 blank frames, discards
outputs, and closes it; production uses a fresh instance. This is a startup
throughput estimate, not an accuracy test or a sustained-load benchmark.
Memory uses host `sysconf` and is unknown where unavailable; accelerator
probing currently covers OpenCV CUDA devices only. Other accelerators are
unverified, and an accelerator entry does not imply detector support. Explicit
CLOUD skips local model construction/probing entirely.

## Server/core integration boundaries

The checked-in server supports signed `POST /ingest` for events/heartbeats and
signed `POST /cameras/{camera_id}/frame` for live-view JPEG storage. **That frame
route performs no inference.** No new server endpoint is assumed or called.

`uplink/inference.py::InferenceUplink` is the missing server-inference seam.
A future adapter receives JPEG bytes, camera ID, stream-relative timestamp,
and optional local event IDs for HYBRID confirmation. Inject it through
`build_pipeline(settings, inference_uplink=adapter)`. A successful submission
means accepted, not confirmed. The default CLI has no adapter: CLOUD/HYBRID
report `cloud_available=false` and `degraded=true`; they do not pretend that
live-view uploads provide fall detection. HYBRID can still send local events.

Remaining contracts to agree with core/server:

- Authenticated inference submission and acceptance semantics, frame IDs,
  timestamps, idempotency, and confirmation/event correlation.
- Server-side detector execution and a confirmation-result contract. The
  current interface submits work only; it does not invent confirmation results.
- Capability and detailed health registration. These reports are currently
  available locally/logged; core's existing heartbeat fields stay unchanged.
- Any durable frame replay/retention contract. Frames currently drop on failure
  and never enter the event spool, matching the existing live-view behavior.

`mantau-core` supplies `Detector`, `FallEvent`, signed `Envelope`, `Heartbeat`,
resilience helpers, and `DurableSpool`. MediaPipe continues through its existing
adapter, which requires `mantau.api.streaming.StreamingDetector` from the
optional detection dependency. Explicit EDGE/HYBRID retain its actionable
ImportError when missing; AUTO records the error and selects CLOUD.

NullDetector remains silent by default. Its synthetic trigger is still usable
in standalone `DetectRunner` tests, but **the production router suppresses all
NullDetector events and events marked `signals.synthetic`**, counts them in
health, and never sends them for alerts or cloud confirmation. The current
server has no safe synthetic-alert channel.

## Install and run

Requires Python 3.10-3.12 and the sibling `../mantau-core` checkout.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m mantau_agent.main
```

First run uses the existing enrollment/camera setup wizard. For unattended
operation, enroll with the server first (see `../mantau-server/README.md`), then:

```powershell
$env:MANTAU_SERVER_URL = "http://localhost:8100"
$env:MANTAU_AGENT_ID = "agent-1"
$env:MANTAU_AGENT_SECRET = "<enrollment secret>"
$env:MANTAU_CAMERA_ID = "cam-1"
$env:MANTAU_CAMERA_HOST = "192.168.1.42"
$env:MANTAU_INFERENCE_MODE = "AUTO"
.venv\Scripts\python.exe -m mantau_agent.main
```

Without a real detector or server-inference adapter, the default configuration
provides capture, live view, and health, **not production fall detection**.
Packaging instructions for Windows/Linux are in `packaging/README.md`.

## Configuration

All settings use the `MANTAU_` prefix and the existing environment/`.env` loading.
Rates and queue bounds must be positive and finite; use the enabled flag to
turn off live view. No dependency changes are required.

| Setting (omit `MANTAU_` below) | Default | Purpose |
|---|---|---|
| `SERVER_URL` | `http://localhost:8100` | Existing server base URL |
| `AGENT_ID`, `AGENT_SECRET` | required | Enrollment identity/secret |
| `CAMERA_ID`, `CAMERA_HOST`, `CAMERA_PORT` | `cam-1`, empty, `554` | Camera identity/address |
| `CAMERA_MAIN_PATH`, `CAMERA_SUB_PATH` | `/stream1`, unset | Existing RTSP paths |
| `CAMERA_USERNAME`, `CAMERA_PASSWORD` | unset | Camera credentials |
| `USE_ONVIF_DISCOVERY` | `false` | Discover a host; fall back to manual config on no result/network error |
| `DEFAULT_STREAM_PROFILE` | `sub` | Existing sub/main selection |
| `INFERENCE_MODE` | `AUTO` | `EDGE`, `CLOUD`, `HYBRID`, `AUTO` |
| `DETECTOR_BACKEND` | `null` | `null` or `mediapipe` |
| `NULL_DETECTOR_TRIGGER_EVERY` | unset | Synthetic testing only; router suppresses outputs |
| `DETECTION_FPS` | `5` | Detection frame-sampling cap and AUTO throughput target |
| `SAMPLER_KEEP_EVERY_N`, `SAMPLER_MAX_FPS` | `1`, unset | Additional legacy detection decimation |
| `CLOUD_UPLOAD_FPS` | `1` | Cloud frame sampling/upload-attempt cap |
| `HYBRID_CONFIRMATION_FPS` | `0.2` | At most one confirmation attempt per 5 seconds; also capped by cloud FPS |
| `LIVE_VIEW_ENABLED`, `LIVE_VIEW_FPS` | `true`, `4` | Independent live-view path (suppressed in EDGE) |
| `LIVE_VIEW_JPEG_QUALITY`, `LIVE_VIEW_MAX_WIDTH` | `70`, `640` | Shared FrameUplink JPEG encoder settings |
| `FRAME_QUEUE_SIZE` | `2` | Maximum pending frames per detection/cloud/live queue |
| `UPLOAD_TIMEOUT_S` | `5` | Per frame-upload timeout |
| `POLL_INTERVAL_S` | `0.02` | Capture handoff polling interval |
| `TUNNEL_PROVIDER` | `null` | Existing direct or `tailscale` transport |
| `SEQ_PATH`, `SPOOL_PATH` | `data/seq.txt`, `data/spool.db` | Persistent sequence and event/heartbeat spool |
| `SPOOL_TTL_S` | `300` | Existing core TTL; expired envelopes evicted during send/replay |
| `HEARTBEAT_INTERVAL_S` | `30` | Health and opportunistic spool-recovery cadence |

Queues drop oldest pending frames to retain recent work. Each worker holds at
most one additional active frame; capture retains one latest frame. Queue bounds
count frames, not bytes. Detection, JPEG encoding, and RTSP capture run off the
event loop. Upload attempts (including failures) are rate limited using monotonic
time; switching modes does not reset that budget. Slow consumers cannot grow
frame memory without bound. Sampled frames with duplicate/older capture
timestamps are ignored.

## Lifecycle and health

```python
pipeline = await build_pipeline(settings, inference_uplink=adapter)
try:
    await pipeline.start()             # idempotent
    await pipeline.change_mode(InferenceMode.HYBRID)
    status = pipeline.health()         # JSON-serializable local diagnostics
finally:
    await pipeline.shutdown()          # idempotent, including before start
```

Mode changes pause admission, discard pending frames, wait for active work,
and then apply the selected mode. Shutdown stops capture, finishes active
inference/event delivery, joins workers, closes detectors/clients/SQLite, and
brings down the tunnel. Queued frames are disposable; failed or cancelled event
sends retain signed envelopes for replay. Restart after shutdown uses a new
pipeline. Detector `push`/`close` must return: a hung native inference call
cannot safely be killed in a Python thread, so shutdown waits for it rather
than closing its resources concurrently. RTSP open/read calls request 1.5s
native timeouts; a capture that fails to stop within 2s reports a shutdown error.

Existing heartbeats carry camera reachability, real local detector status
(false for CLOUD/NullDetector), and combined pending-frame/spool depth. Detailed
local health includes mode/reason, cloud availability, degradation, per-path
queue sizes/drops/upload failures, synthetic suppression, and last worker error.
Capabilities log at startup; routing health logs at each heartbeat. Replay
remains opportunistic on successful event/heartbeat sends.

## Tests and verification limits

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

The suite exercises all modes, deterministic AUTO/weak-device selection,
serialization/platform identification, independent rates, bounded queues,
synthetic suppression, upload errors/timeouts, durable event recovery, discovery
fallback, mode changes, and shutdown. Existing RTSP/ONVIF socket, HMAC golden
contract, SQLite, and setup/factory tests remain in the full suite. Tests use
local sockets, mock HTTP, and injected detectors; no camera/server/account is
required. On Windows sandboxes with owner-only pytest directory restrictions,
run tests with normal filesystem permissions and a fresh `--basetemp` directory.

Real Linux ARM/Raspberry Pi hardware, MediaPipe model accuracy/performance,
ONVIF multicast, Tailscale, Docker/binary packaging, and server inference are
not validated by this suite. ONVIF discovery only locates hosts; Media-service
`GetProfiles`/`GetStreamUri` negotiation remains unimplemented. Configured RTSP
paths and credentials are reused. No changes to core, server, or app are needed
for the existing event/live-view/heartbeat contracts.
