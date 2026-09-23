"""Interactive enrollment and validated camera setup with durable state."""

from __future__ import annotations

import asyncio
import getpass
import secrets
import socket
import sys
from pathlib import Path

import httpx
from mantau_core.contracts import CameraRef, Credentials, StreamProfile

from .camera.validate import validate_camera
from .capabilities import InferenceMode
from .config import Settings
from .discovery.service import DiscoveryCandidate, discover_cameras
from .state import (
    AgentConfiguration, CameraConfiguration, ConfigurationStore,
    EnrollmentConfiguration, SetupState,
)

# Retained for callers migrating from the original .env wizard.
ENV_PATH = Path(".env")


def needs_setup(settings: Settings | None = None,
                configuration: AgentConfiguration | None = None) -> bool:
    if configuration is not None:
        if settings is not None and settings.command_channel_enabled and configuration.enrollment.agent_secret:
            return False
        return configuration.setup_state is not SetupState.COMPLETE or configuration.camera is None
    s = settings or Settings()
    return not (s.agent_id and s.agent_secret and (s.camera_host or s.use_onvif_discovery))


def default_agent_id() -> str:
    """Hostname plus a random suffix: identical images (every Pi is
    `raspberrypi`) must not collide, because enrollment is create-only."""
    host = socket.gethostname().lower()
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in host).strip("-")
    return f"agent-{(cleaned or 'device')[:40]}-{secrets.token_hex(3)}"


class AgentIdTaken(RuntimeError):
    """Another agent already enrolled under this id (HTTP 409)."""


class AlreadyClaimed(RuntimeError):
    """The agent belongs to a household; it gets no more claim codes."""


def _proof(agent_id: str, secret: str) -> dict[str, str]:
    return {"X-Mantau-Agent-ID": agent_id, "X-Mantau-Agent-Secret": secret}


def enroll(server_url: str, agent_id: str, *, current_secret: str | None = None,
           client: httpx.Client | None = None) -> str:
    """Enroll a new identity, or rotate this one's secret when
    `current_secret` proves it. Returns the new secret; prints a claim code
    when the agent is still unclaimed. Never prints the secret."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.post(
            f"{server_url.rstrip('/')}/agents/enroll", json={"agent_id": agent_id},
            headers=_proof(agent_id, current_secret) if current_secret else None,
        )
        if resp.status_code == 409:
            raise AgentIdTaken(agent_id)
        resp.raise_for_status()
        if resp.json().get("claim_code"):
            print(f"Claim code: {resp.json()['claim_code']}")
        return resp.json()["secret"]
    finally:
        if owns_client:
            client.close()


def refresh_claim_code(server_url: str, agent_id: str, secret: str, *,
                       client: httpx.Client | None = None) -> str:
    """A fresh single-use claim code. Does not change the agent secret."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.post(f"{server_url.rstrip('/')}/agent-control/claim-code",
                           headers=_proof(agent_id, secret))
        if resp.status_code == 409:
            raise AlreadyClaimed(agent_id)
        resp.raise_for_status()
        return resp.json()["claim_code"]
    finally:
        if owns_client:
            client.close()


def rotate_secret(store: ConfigurationStore, *, client: httpx.Client | None = None) -> None:
    """Replace the saved secret, proving the current one. Household
    ownership is unchanged; the old secret stops working immediately."""
    configuration = store.load()
    if configuration is None or not configuration.enrollment.agent_secret:
        raise RuntimeError("This agent is not enrolled yet; run `mantau-agent setup`.")
    enrollment = configuration.enrollment
    secret = enroll(enrollment.server_url, enrollment.agent_id,
                    current_secret=enrollment.agent_secret, client=client)
    store.save(configuration.model_copy(update={
        "enrollment": enrollment.model_copy(update={"agent_secret": secret}),
    }))


def render_env_file(*, server_url: str, agent_id: str, secret: str, camera: dict) -> str:
    """Compatibility helper for existing Docker/.env automation."""
    lines = [
        f"MANTAU_SERVER_URL={server_url}", f"MANTAU_AGENT_ID={agent_id}",
        f"MANTAU_AGENT_SECRET={secret}", f"MANTAU_CAMERA_ID={agent_id}-cam",
        f"MANTAU_CAMERA_HOST={camera['host']}", f"MANTAU_CAMERA_PORT={camera['port']}",
        f"MANTAU_CAMERA_MAIN_PATH={camera['main_path']}",
    ]
    if camera.get("sub_path"):
        lines.extend((f"MANTAU_CAMERA_SUB_PATH={camera['sub_path']}",
                      "MANTAU_DEFAULT_STREAM_PROFILE=sub"))
    if camera.get("username"):
        lines.extend((f"MANTAU_CAMERA_USERNAME={camera['username']}",
                      f"MANTAU_CAMERA_PASSWORD={camera.get('password', '')}"))
    return "\n".join(lines) + "\n"


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or default


def _choose_host(candidates: list[DiscoveryCandidate]) -> str:
    if len(candidates) == 1:
        host = candidates[0].host
        print(f"Found one camera at {host}.")
        return host
    if candidates:
        print("Found multiple cameras. Select one explicitly:")
        for index, candidate in enumerate(candidates, 1):
            state = "RTSP reachable" if candidate.rtsp_reachable else (
                f"RTSP {candidate.reachability_error or 'unreachable'}")
            print(f"  {index}. {candidate.host} ({state})")
        print(f"  {len(candidates) + 1}. Enter an address manually")
        while True:
            choice = _prompt(f"Camera number (1-{len(candidates) + 1})")
            if choice.isdigit() and 1 <= int(choice) <= len(candidates) + 1:
                selected = int(choice) - 1
                return candidates[selected].host if selected < len(candidates) else ""
            print("Enter one of the displayed numbers; no camera was selected.")
    print("No ONVIF camera was found; enter the RTSP camera manually.")
    return ""


def _camera_ref(camera_id: str, camera: dict) -> tuple[CameraRef, StreamProfile]:
    paths = {StreamProfile.MAIN: camera["main_path"]}
    if camera.get("sub_path"):
        paths[StreamProfile.SUB] = camera["sub_path"]
    credentials = None
    if camera.get("username"):
        credentials = Credentials(username=camera["username"], password=camera.get("password", ""))
    profile = StreamProfile.SUB if camera.get("sub_path") else StreamProfile.MAIN
    return CameraRef(
        camera_id=camera_id, name=camera_id, host=camera["host"],
        port=int(camera["port"]), paths=paths, credentials=credentials,
    ), profile


async def _pick_camera_interactive(agent_id: str) -> CameraConfiguration:
    template = CameraRef(
        camera_id=f"{agent_id}-cam", name=f"{agent_id}-cam", host="127.0.0.1",
        paths={StreamProfile.MAIN: "/stream1", StreamProfile.SUB: "/stream2"},
    )
    print("\nLooking for cameras on this network (ONVIF discovery, 3s)...")
    try:
        candidates = await discover_cameras(template, profile=StreamProfile.SUB)
    except OSError as exc:
        print(f"  (discovery unavailable: {type(exc).__name__})")
        candidates = []
    host = _choose_host(candidates)

    while True:
        host = host or _prompt("Camera IP address or hostname")
        port_text = _prompt("RTSP port", default="554")
        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            print("RTSP port must be a number from 1 to 65535.")
            host = ""
            continue
        main_path = _prompt("RTSP path for the main stream", default="/stream1")
        sub_path = _prompt("RTSP path for the low-bitrate/substream (blank if none)")
        username = _prompt("Camera username (blank if none)")
        password = getpass.getpass("Camera password: ") if username else ""
        camera = {
            "host": host, "port": port, "main_path": main_path,
            "sub_path": sub_path or None, "username": username or None,
            "password": password or None,
        }
        camera_ref, profile = _camera_ref(f"{agent_id}-cam", camera)
        print(f"Validating the {'substream' if profile is StreamProfile.SUB else 'main stream'}...")
        failure = await validate_camera(camera_ref, profile=profile)
        if failure is None:
            return CameraConfiguration(
                camera_id=camera_ref.camera_id, default_stream_profile=profile.value, **camera)
        print(f"Camera was not saved: {failure}")
        print("Check the address, RTSP paths, and credentials, then try again.")
        host = ""


_NON_INTERACTIVE_MESSAGE = (
    "Setup is incomplete and requires an interactive terminal. Run "
    "`mantau-agent setup` once, or provide MANTAU_AGENT_ID, "
    "MANTAU_AGENT_SECRET, and MANTAU_CAMERA_HOST for Docker/CI."
)


def run_wizard(store: ConfigurationStore | None = None, *, remote: bool = False) -> AgentConfiguration:
    store = store or ConfigurationStore()
    if not sys.stdin.isatty():
        raise RuntimeError(_NON_INTERACTIVE_MESSAGE)
    existing = store.load()
    print("=== Mantau agent setup ===\n")
    try:
        if existing is not None and existing.enrollment.agent_secret:
            enrollment = existing.enrollment
            print(f"Reusing enrollment for {enrollment.agent_id!r}.")
        else:
            server_url = _prompt("Mantau server URL", default="http://localhost:8100")
            while True:
                agent_id = _prompt("A name for this device", default=default_agent_id())
                print(f"Registering {agent_id!r} with the server...")
                try:
                    secret = enroll(server_url, agent_id)
                    break
                except AgentIdTaken:
                    print("That name is already used by another device; choose another.")
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    raise RuntimeError(f"Enrollment failed: {type(exc).__name__}") from exc
            enrollment = EnrollmentConfiguration(
                server_url=server_url, agent_id=agent_id, agent_secret=secret)
            store.save(AgentConfiguration(
                setup_state=SetupState.ENROLLED, enrollment=enrollment,
                inference_mode=InferenceMode.AUTO,
            ))
        if remote:
            configuration = existing or store.load()
            print("Enter the claim code in Mantau app, then start `mantau-agent run` to finish camera setup from the phone.")
            return configuration
        camera = asyncio.run(_pick_camera_interactive(enrollment.agent_id))
        while True:
            raw_mode = _prompt("Inference mode (AUTO/EDGE/CLOUD/HYBRID)", default="AUTO").upper()
            try:
                inference_mode = InferenceMode(raw_mode)
                break
            except ValueError:
                print("Choose AUTO, EDGE, CLOUD, or HYBRID.")
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError("Setup cancelled; saved enrollment can be resumed") from exc
    configuration = AgentConfiguration(
        setup_state=SetupState.COMPLETE, enrollment=enrollment,
        camera=camera, inference_mode=inference_mode,
    )
    store.save(configuration)
    print(f"Configuration validated and saved to {store.path}.")
    return configuration
