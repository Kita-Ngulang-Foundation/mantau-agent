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

The agent benchmarks architecture, memory, Android GPU/NNAPI API availability,
detector availability/latency, and thermal state. It maps those facts into the
existing v1 capability fields and reports the selected mode and explanation in
the existing status/health fields. It does not add Android-only wire fields.

`AUTO` and `CLOUD` are currently advertised. CLOUD decodes the RTSP H.264
substream locally and sends signed sampled JPEGs through the existing
`POST /cameras/{camera_id}/frame` protocol. Defaults are bounded to 1 FPS, 640
pixels wide, JPEG quality 65, and 256 KiB per frame (with a hard 2 FPS ceiling).
The work queue keeps at most two latest frames. Frames that exceed limits, age
out, or encounter an outage are explicitly discarded; live frames are never
spooled for later upload.

EDGE is implemented behind an interchangeable detector interface, including
event cooldown/deduplication and exact Linux/Pi fall-event mapping, but is not
advertised or selectable because this repository contains no verified,
redistributable mobile fall-detection model. Enabling it requires a
`fall_detection.tflite` artifact plus its source URL, SPDX license, SHA-256,
input tensor specification, and documented fall-output semantics. The
unavailable detector never fabricates a fall.

HYBRID has a bounded one-confirmation-frame-per-event policy with a five-second
minimum interval, but remains unavailable because there is also no
event-correlated server-confirmation endpoint in the shared contract. It never
uses continuous full-quality video. Unsupported EDGE/HYBRID commands return
the shared `unsupported` result. AUTO falls back to CLOUD for missing/failed
detectors or thermal pressure and reports a precise explanation.

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
and inference-mode transitions. Real ONVIF cameras, MediaCodec/vendor RTSP
variants, device power management, and control-plane integration still require
hardware testing on the target phone and LAN.
