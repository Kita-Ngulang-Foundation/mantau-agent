# Packaging mantau-agent as a standalone binary

Turns "clone the repo, create a venv, pip install, run a module" into
"download one file and run it" — no Python, git, or pip required on the
target device. This is what actually makes the agent installable on a
Raspberry Pi or a spare Linux/Windows box, without setting up a development
environment. Android is a platform identifier only; no Android runtime is built
or validated here.

`entrypoint.py` is the single script PyInstaller freezes; it just calls
`mantau_agent.main.main()`, which runs the first-run setup wizard
(`setup_wizard.py`) on a fresh device and the normal agent loop after that.

## Windows (build natively, on a Windows machine)

```powershell
cd mantau-agent
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --onefile --name mantau-agent-windows-x64 `
  --distpath dist\windows --workpath build\pyi-windows --specpath build `
  packaging\entrypoint.py
```

Output: `dist\windows\mantau-agent-windows-x64.exe`.

## Linux x86_64 and ARM64 (build via Docker — cross-builds via buildx+QEMU)

Docker Desktop ships buildx with QEMU emulation already registered, so
building for `arm64` from an x86_64 host works out of the box — just much
slower than a native build (expect it to take significantly longer than
the x86_64 build, since every instruction is emulated).

```powershell
cd mantau-agent

# x86_64 (a generic Linux VM/box)
docker build --platform linux/amd64 -f packaging\Dockerfile.pyinstaller -t mantau-agent-pyi:amd64 ..
docker run --rm -v "${PWD}\dist\linux-x64:/out" mantau-agent-pyi:amd64 --name mantau-agent-linux-x64

# arm64 (Raspberry Pi and generic Linux ARM64; Android is not validated)
docker build --platform linux/arm64 -f packaging\Dockerfile.pyinstaller -t mantau-agent-pyi:arm64 ..
docker run --rm -v "${PWD}\dist\linux-arm64:/out" --platform linux/arm64 mantau-agent-pyi:arm64 --name mantau-agent-linux-arm64
```

The build context (`..`) is `mantau-prototype/`, the same as the real
`Dockerfile` — it needs the `mantau-core` sibling package too.

## Verifying a built binary without the real target hardware

Run it in a fresh, unrelated base image with the required env vars set —
if it stays running (rather than crashing), the freeze picked up every
dependency it needs:

```powershell
docker run --rm --platform linux/amd64 -v "${PWD}\dist\linux-x64:/bin/agent" `
  -e MANTAU_AGENT_ID=agent-test -e MANTAU_AGENT_SECRET=testsecret -e MANTAU_CAMERA_HOST=192.0.2.1 `
  debian:12-slim timeout 5 /bin/agent/mantau-agent-linux-x64
```

Exit code `124` (timeout killed a still-running process) means success;
anything else means the freeze is missing something.

## What's NOT built here

Android packaging and execution are deferred. The agent can identify Android
in a capability report, but this repository does not provide an APK,
foreground service, or a validated Termux installation. The Linux ARM64 build
must not be treated as a tested Android binary.
