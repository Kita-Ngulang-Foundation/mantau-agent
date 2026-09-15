First standalone releases of mantau-agent — download, run, done. No Python,
git, or pip required on the target device.

## Downloads

| Device | File |
|---|---|
| Windows PC | `mantau-agent-windows-x64.exe` |
| Raspberry Pi (or any other Linux ARM64 board) | `mantau-agent-linux-arm64` |
| Generic Linux PC/laptop/VM | `mantau-agent-linux-x64` |
| Old/unused Android phone (via [Termux](https://f-droid.org/packages/com.termux/)) | `mantau-agent-linux-arm64` |

## First run

No manual setup required — the agent walks you through it:

1. Run the binary (`chmod +x` first on Linux/Android).
2. It asks for your Mantau server's URL and a name for this device, then
   registers itself automatically (no `curl`/API calls needed).
3. It looks for cameras on your network (ONVIF discovery) and lets you pick
   one, or type an IP address in manually if none are found.
4. Everything is saved — future restarts start monitoring immediately,
   no prompts.

## What's new

- First-run interactive setup wizard: self-enrollment + camera discovery,
  replacing manual `curl POST /agents/enroll` + hand-set environment
  variables.
- Fixed a crash when the agent's data directory doesn't already exist
  (affected any install outside Docker, which normally creates it via the
  volume mount).

## Verified

All three binaries were run in a fresh, unrelated base container with zero
Python installed and confirmed to start and stay running — proving each
freeze is genuinely self-contained, not just "built without errors."
