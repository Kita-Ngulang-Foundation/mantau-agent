# Mantau Agent 0.1.0

The standalone agent release targets unattended Linux installations. Select
the artifact that matches the host:

| Deployment | File |
|---|---|
| Linux x86_64 PC, laptop, or VM | `mantau-agent-linux-x64` |
| Linux ARM64, including 64-bit Raspberry Pi OS | `mantau-agent-linux-arm64` |

Install the binary with `packaging/install.sh`. The installer creates a
restricted service account, launches enrollment and validated camera setup on
first install, and enables the systemd service. Subsequent boots and process
failures are handled without an interactive login.

This release includes ONVIF discovery with explicit selection, manual RTSP
fallback, substream preference, durable configuration and uplink spooling,
bounded reconnect backoff, local JSON discovery/status commands, and health
reporting. Android packaging and runtime support are outside this release.

Before publishing either artifact, run it on the target architecture and
verify setup, RTSP capture, server outage recovery, restart behavior, and
systemd shutdown on representative hardware.
