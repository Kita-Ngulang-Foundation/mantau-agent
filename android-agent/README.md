# Mantau Android Agent

This is the native Android counterpart to the Linux/Raspberry Pi agent. Install
it on a spare phone that remains at home on the CCTV LAN. It is not
`mantau-app`: the Flutter app remains the OS-agnostic family/control plane and
does not gain RTSP, ONVIF, inference, or monitoring services.

The Android agent uses the existing v1 control contracts and endpoints:

- enrollment and claim code: `POST /agents/enroll`;
- authenticated command/status polling: `POST /agent-control/commands/poll`;
- durable command results: `POST /agent-control/commands/{id}/results`;
- `platform` is always `android` in status and capability reports.

The agent reports architecture, memory, Android GPU/NNAPI API availability,
thermal state, and server inference availability through the existing v1
capability fields. It does not initialize or benchmark an on-device model.

The runtime uses CLOUD only; old saved EDGE/HYBRID choices are migrated to
CLOUD at service start. Capability reports advertise CLOUD when the server's
`GET /inference/capability` says inference is available (re-read every
heartbeat). AUTO is retained for protocol compatibility and also uses CLOUD.
CLOUD decodes the RTSP
H.264 substream locally and sends signed JPEGs to `POST /agents/{id}/inference`
(`uplink/InferenceUplink.kt`, the same contract and golden signature as the
Python agent) at 10 fps capped by the server's `max_fps`; the server runs the
fall detector and stores/pushes falls under this agent. Live view keeps its own
1 FPS upload to `POST /cameras/{camera_id}/frame`. Frames are 640 pixels wide at
most, refused above the server's size limit, retried once on a network/5xx
error with the same frame id, dropped once stale, and never spooled.

The local fall detector, activity engine, and shared parity tests remain in
source, but the monitoring service does not load or call them. If server
inference is unavailable, monitoring reports degraded health and drops
inference frames until the server is reachable again. The camera connection,
control channel, heartbeat, and live-view path continue to operate.

## Build

Requirements: JDK 17 or 21 and Android SDK 36. Set `ANDROID_HOME` or create a
local, untracked `local.properties` containing `sdk.dir=...`.

From `mantau-agent/android-agent`:

```powershell
$env:JAVA_HOME = 'C:\Program Files\Microsoft\jdk-21.0.12.101-hotspot'
.\gradlew.bat testDebugUnitTest assembleDebug
```

On Linux/macOS:

```sh
./gradlew testDebugUnitTest assembleDebug
```

The debug APK is written to
`app/build/outputs/apk/debug/app-debug.apk`.

## Install and enroll

Enable developer options and USB debugging on the spare phone, then install:

```powershell
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

The phone must run Android 8.0/API 26 or newer. In the app:

1. Enter the control-plane URL and a recognizable agent name, then tap
   **Enroll / refresh claim code**. Re-enrollment is explicitly confirmed
   because it rotates the agent secret.
2. Enter the displayed claim code in `mantau-app`.
   Tap **Start** on the Android Agent even if no camera is configured yet.
   The foreground service polls setup commands while the camera is unconfigured;
   its activity may then be closed while setup continues from the family phone.
3. Connect the phone to the same local Wi-Fi as the CCTV. Run ONVIF discovery
   and explicitly choose among multiple cameras, or enter the camera IP, RTSP
   port, paths, and credentials manually.
4. Put the low-bitrate camera path in the substream field. It is preferred when
   configured; the main path is the fallback.
5. Save and start monitoring. Allow notification permission so the persistent
   status is visible in the notification drawer. If permission is denied,
   Android still runs the foreground service and exposes it in Active apps.
6. For an unattended phone, keep it powered and set Mantau Agent battery usage
   to unrestricted if that vendor's Android build aggressively stops apps.

For a real deployment use HTTPS. Cleartext HTTP is enabled in this prototype so
a development server on the home LAN can be used.

## Runtime and security behavior

- The foreground service is declared as `connectedDevice`, starts only from the
  visible activity, posts a persistent low-importance notification, and holds a
  partial wake lock only while monitoring.
- The generated agent ID and non-secret metadata survive process/device restarts
  in private app preferences. The enrolled secret, camera password, and
  credential-bearing in-flight commands are AES-GCM encrypted with a
  non-exportable Android Keystore key.
- WS-Discovery is allowed only when an active Wi-Fi network exists. Its UDP
  socket is bound to that network. A non-reference-counted `MulticastLock` is
  held only around the bounded discovery exchange and released in `finally`,
  including timeout, cancellation, and parse/socket failure.
- Discovery parses every `ProbeMatch`, deduplicates by camera host, returns all
  candidates, and never silently selects the first of several cameras. Manual
  IP/RTSP setup remains available because discovery does not implement ONVIF
  Media `GetProfiles`/`GetStreamUri` negotiation.
- RTSP uses TCP interleaving, Basic or MD5 Digest authentication, the configured
  substream preference, and H.264 RTP access-unit assembly. A two-frame
  drop-oldest buffer bounds memory. Camera state, completed-frame time, and
  reconnect count are persisted without credentials. Reconnect uses capped
  full-jitter exponential backoff.
- H.264 access units are decoded before the bounded JPEG upload queue drops
  frames. A silent stream times out after ten seconds and reconnects; JPEGs
  older than five seconds are discarded before upload.
- CLOUD frames count as uploaded only when the server processed them; falls it
  detects are alerted by the server, not by this device.
- A delivered command is encrypted and persisted before the agent submits its
  `running` result. Final results are persisted before submission and replayed
  on redelivery, matching the Linux/Pi custody and idempotency behavior.
- Heartbeats and semantic events use the same canonical HMAC envelope as the
  Python agent. Sequence allocation is persisted before use. A bounded,
  app-private, fsynced queue retains unacknowledged envelopes across restarts;
  acknowledgements remove them atomically, so a recovered event keeps its
  original sequence and signature and server-side deduplication prevents a
  duplicate delivery.
- Inference-mode commands update the requested mode durably. The effective mode
  is re-evaluated against detector health and thermal pressure, and status
  reports requested mode, effective mode, reason, queue depth, and disposable
  frame-upload counters without exposing credentials.

Stopping the service releases the RTSP socket and wake lock and removes the
foreground notification. Android app-data removal intentionally erases the
identity and Keystore-protected secrets; the device must then be re-enrolled.

## Tests

`testDebugUnitTest` covers configuration validation, exact shared-fixture field
compatibility, exact Python-agent golden envelope/signature compatibility,
service lifecycle state, multicast-lock cleanup, multi-camera discovery
parsing/deduplication, RTSP state transitions, latest-frame and upload queue
bounds, reconnect behavior, signed frame requests, disposable outage behavior,
durable queue recovery, cooldown/deduplication, detector and thermal fallback,
and inference-mode transitions. `FallParityTest` replays mantau-core's recorded
pose sequences (copied into `src/test/resources/pose_sequences`) through the
Kotlin rules and the real ONNX model and requires the exact per-frame decisions
the Python rules recorded; `ModelAssetsTest` checks the bundled models and
fixture copies against mantau-core. `ObservationParityTest` and
`ActivityParityTest` replay mantau-core's observation and activity fixtures
(`src/test/resources/activity_sequences`) and require exactly the events the
Python rules produce.

`connectedDebugAndroidTest` runs the real MediaPipe + ONNX Runtime path on a
device or emulator. Generate its frames first with
`scripts/prepare-instrumentation-frames.sh` (third-party footage, not committed).
`StagedScenesTest` replays scenes staged by `integration/stage_activity_clips.py`
(pass `e2eScene` and base64 `e2eSettings`; with `e2eServer` and agent
credentials it also posts the signed events to a running server). Real ONVIF cameras, MediaCodec/vendor RTSP
variants, device power management, and control-plane integration still require
hardware testing on the target phone and LAN.
