"""First-run interactive setup -- self-enrolls with the server, discovers
or manually configures the camera, and writes a `.env` file so no future
run ever asks again. This is what makes "download the binary and run it"
actually true, instead of someone hand-running a `curl POST /agents/enroll`
and setting six environment variables themselves.

`needs_setup()` asks `Settings()` (not the `.env` file directly) whether
the required fields are already populated -- `Settings` already merges
`os.environ` and `.env` the same way the real agent run will, so a
Docker/compose deployment that sets `MANTAU_*` env vars directly is
correctly recognized as "already configured" and never hits the wizard,
even though no `.env` file exists on disk in that case.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import httpx

from .config import Settings
from .discovery.onvif import discover as onvif_discover

ENV_PATH = Path(".env")


def needs_setup(settings: Settings | None = None) -> bool:
    s = settings or Settings()
    return not (s.agent_id and s.agent_secret and (s.camera_host or s.use_onvif_discovery))


def default_agent_id() -> str:
    """`hostname`-derived so two devices don't collide by default, and so
    the name shown in `/agents` is recognizable without asking the user to
    invent one."""
    host = socket.gethostname().lower()
    # Keep it to characters an operator would expect in an id; a raw
    # hostname can contain spaces/underscores on some platforms.
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in host).strip("-")
    return f"agent-{cleaned or 'device'}"


def enroll(server_url: str, agent_id: str, *, client: httpx.Client | None = None) -> str:
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.post(f"{server_url.rstrip('/')}/agents/enroll", json={"agent_id": agent_id})
        resp.raise_for_status()
        return resp.json()["secret"]
    finally:
        if owns_client:
            client.close()


def render_env_file(*, server_url: str, agent_id: str, secret: str, camera: dict) -> str:
    """Pure formatting, split out from `run_wizard()` so the actual file
    contents are unit-testable without a real terminal or network."""
    lines = [
        f"MANTAU_SERVER_URL={server_url}",
        f"MANTAU_AGENT_ID={agent_id}",
        f"MANTAU_AGENT_SECRET={secret}",
        f"MANTAU_CAMERA_ID={agent_id}-cam",
        f"MANTAU_CAMERA_HOST={camera['host']}",
        f"MANTAU_CAMERA_PORT={camera['port']}",
        f"MANTAU_CAMERA_MAIN_PATH={camera['main_path']}",
    ]
    if camera.get("sub_path"):
        lines.append(f"MANTAU_CAMERA_SUB_PATH={camera['sub_path']}")
        lines.append("MANTAU_DEFAULT_STREAM_PROFILE=sub")
    if camera.get("username"):
        lines.append(f"MANTAU_CAMERA_USERNAME={camera['username']}")
        lines.append(f"MANTAU_CAMERA_PASSWORD={camera.get('password', '')}")
    return "\n".join(lines) + "\n"


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or default


def _pick_camera_interactive() -> dict:
    print("\nLooking for cameras on this network (ONVIF discovery, 3s)...")
    try:
        devices = onvif_discover(timeout_s=3.0)
    except OSError as exc:
        print(f"  (discovery unavailable: {exc})")
        devices = []
    # De-dup while keeping first-seen order -- multiple ProbeMatch replies
    # from the same device on a noisy network shouldn't show up twice.
    hosts = list(dict.fromkeys(d.host for d in devices if d.host))

    host = ""
    if hosts:
        print("Found:")
        for i, h in enumerate(hosts, 1):
            print(f"  {i}. {h}")
        print(f"  {len(hosts) + 1}. Enter manually")
        choice = _prompt(f"Pick a camera (1-{len(hosts) + 1})", default="1")
        idx = int(choice) - 1 if choice.isdigit() else len(hosts)
        if 0 <= idx < len(hosts):
            host = hosts[idx]
    if not host:
        if not hosts:
            print("No cameras found automatically (this is normal if the camera "
                  "doesn't support ONVIF, or the network blocks multicast).")
        host = _prompt("Camera IP address")

    port = _prompt("RTSP port", default="554")
    main_path = _prompt("RTSP path for the main stream", default="/stream1")
    sub_path = _prompt("RTSP path for a lower-bandwidth sub stream (blank if none)")
    username = _prompt("Camera username (blank if none)")
    password = _prompt("Camera password") if username else ""
    return {
        "host": host, "port": port, "main_path": main_path,
        "sub_path": sub_path, "username": username, "password": password,
    }


_NON_INTERACTIVE_MESSAGE = (
    "This device isn't enrolled yet, and setup needs an interactive "
    "terminal (it wasn't given one -- e.g. running under Docker without "
    "-it, or as a background service).\n"
    "Either run this once interactively first, or set "
    "MANTAU_AGENT_ID / MANTAU_AGENT_SECRET / MANTAU_CAMERA_HOST "
    "(and friends) directly as environment variables."
)


def run_wizard(env_path: Path = ENV_PATH) -> None:
    if not sys.stdin.isatty():
        print(_NON_INTERACTIVE_MESSAGE, file=sys.stderr)
        sys.exit(1)

    print("=== Mantau agent -- first-time setup ===\n")
    print("This only runs once. Everything below is saved so future restarts")
    print("start monitoring immediately.\n")

    try:
        server_url = _prompt("Mantau server URL", default="http://localhost:8100")
        agent_id = _prompt("A name for this device", default=default_agent_id())

        print(f"\nRegistering '{agent_id}' with {server_url} ...")
        try:
            secret = enroll(server_url, agent_id)
        except httpx.HTTPError as exc:
            print(f"\nCould not reach the server: {exc}")
            print("Check the server URL and your network connection, then run this again.")
            sys.exit(1)
        print("Registered.")

        camera = _pick_camera_interactive()
    except EOFError:
        # isatty() can still say True on a technically-console-attached but
        # actually-empty stdin (observed running a frozen exe under a
        # redirected/emulated shell) -- this is the real backstop, not just
        # defensive padding: it's what actually fired in that case.
        print(f"\n{_NON_INTERACTIVE_MESSAGE}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)

    env_path.write_text(
        render_env_file(server_url=server_url, agent_id=agent_id, secret=secret, camera=camera),
        encoding="utf-8",
    )
    print(f"\nSaved to {env_path.resolve()}. Starting monitoring now...\n")
