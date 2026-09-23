# Linux / Raspberry Pi packaging and installation

The agent ships as one PyInstaller binary per Linux architecture. Raspberry Pi
is the Linux ARM64 deployment profile; it does not have a separate codebase.
Builds use Debian Bookworm (glibc 2.36); use Bookworm or newer target systems.

## Build both artifacts

From `mantau-agent`, with Docker buildx and ARM64 emulation available:

```sh
sh packaging/build-linux.sh
```

Outputs:

- `dist/mantau-agent-linux-x64`
- `dist/mantau-agent-linux-arm64`

The build context is the parent `mantau-prototype` directory because the agent
depends on the sibling `mantau-core` package. PyInstaller must build on the same
OS family and architecture it targets; buildx supplies the architecture-specific
Linux environment. Building is not proof that camera codecs or detector
accelerators work on a target, so run the resulting artifact on representative
x86_64 and Raspberry Pi hardware before release.

To build one artifact manually:

```sh
docker buildx build --load --platform linux/amd64 \
  -f packaging/Dockerfile.pyinstaller -t mantau-agent-pyi:amd64 ..
mkdir -p dist
docker run --rm --platform linux/amd64 \
  -v "$PWD/dist:/out" mantau-agent-pyi:amd64 \
  --name mantau-agent-linux-x64
```

Use `linux/arm64`, tag `arm64`, and name `mantau-agent-linux-arm64` for the
Raspberry Pi/generic ARM64 artifact.

## Install

Copy the matching binary plus `packaging/install.sh` and
`packaging/systemd/mantau-agent.service` to the target, then run:

```sh
sudo sh packaging/install.sh ./mantau-agent-linux-x64
```

The idempotent installer:

- validates the host architecture and installs `/usr/local/bin/mantau-agent`;
- creates the locked-down `mantau-agent` system user and group;
- creates `/etc/mantau-agent` and `/var/lib/mantau-agent` as private,
  service-owned directories;
- installs the systemd unit with restart, hardening, and explicit writable paths;
- runs enrollment and displays the claim code when no configuration exists;
- starts the command worker before a camera is configured, so setup can finish
  from the separate Mantau app phone. The private configuration directory is
  writable by the service to persist validated remote camera changes.

For manual enrollment without the installer, use `mantau-agent setup --remote`
followed by `mantau-agent run`. The existing `mantau-agent setup` command retains
interactive local camera validation. File-based enrollment enables command
polling; legacy environment-only installations retain their previous default.

For image creation or another pre-seeded flow, `--no-setup` installs and enables
the unit without starting it when configuration is absent:

```sh
sudo sh packaging/install.sh --no-setup ./mantau-agent-linux-arm64
```

Place a valid `0600` configuration at `/etc/mantau-agent/config.json`, then
start the unit. Environment overrides can be added with a systemd drop-in; do
not place secrets directly in the world-readable unit file.

## Upgrade and uninstall

Run the installer again with a new binary. It stops the service, replaces the
binary and unit, preserves configuration/data, reloads systemd, and starts the
service again.

Default uninstall preserves enrollment, sequence state, pending envelopes, and
status so reinstall/recovery remains possible:

```sh
sudo sh packaging/uninstall.sh
```

Explicit purge removes those directories and the service account:

```sh
sudo sh packaging/uninstall.sh --purge
```

## Safe checks before release

Run the Python suite and shell syntax checks:

```sh
python -m pytest tests -q
sh -n packaging/build-linux.sh
sh -n packaging/install.sh
sh -n packaging/uninstall.sh
```

When Docker is available, build both artifacts and run at least these smoke
checks in a clean Linux container:

```sh
./dist/mantau-agent-linux-x64 --help
./dist/mantau-agent-linux-x64 status --json
```

Repeat the help/status smoke check for ARM64 under an ARM64 runner or buildx/QEMU,
then validate RTSP capture, service restart, and outage spool recovery on the
actual target class.

The native Android Agent is a separate application and release path. See
`../android-agent/README.md`; it does not use these PyInstaller or systemd
artifacts.
