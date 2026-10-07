"""Build the monitoring pipeline from existing camera and uplinks."""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mantau_core.buffer import DurableSpool
from mantau_core.contracts import CameraRef, Credentials, StreamProfile
from mantau_core.activity import ActivityEngine
from mantau_core.resilience import BackoffPolicy

from .activity_rules import build_activity_rules
from .clips import ClipRecorder, ClipUploader
from . import __version__
from .camera.puller import CameraPuller
from .capabilities import InferenceMode, inspect_capabilities
from .config import Settings, load_settings
from .control import CommandExecutor, CompletedCommandStore, ControlPlaneWorker
from .detect.router import InferenceRouter
from .detect.sampler import FrameSampler
from .discovery.service import discover_cameras
from .health.status import StatusStore
from .pipeline import MonitoringPipeline
from .uplink.client import UplinkClient
from .uplink.frames import FrameUplink
from .uplink.inference import (
    HttpInferenceUplink, InferenceUplink, cloud_rate, discover_capability,
)
from .uplink.seq import SeqCounter
from .uplink.spool import EnvelopeSpool
from .uplink.tunnel import NullTunnel, TailscaleTunnel, TunnelProvider


def _build_camera(settings: Settings) -> CameraRef:
    paths: dict[StreamProfile, str] = {StreamProfile.MAIN: settings.camera_main_path}
    if settings.camera_sub_path:
        paths[StreamProfile.SUB] = settings.camera_sub_path
    credentials = None
    if settings.camera_username:
        credentials = Credentials(username=settings.camera_username,
                                   password=settings.camera_password or "")
    return CameraRef(camera_id=settings.camera_id, name=settings.camera_id,
                      host=settings.camera_host, port=settings.camera_port,
                      paths=paths, credentials=credentials)


def _build_tunnel(settings: Settings) -> TunnelProvider:
    if settings.tunnel_provider == "tailscale":
        return TailscaleTunnel()
    return NullTunnel()


async def _discover_camera(settings: Settings) -> CameraRef:
    camera = _build_camera(settings)
    if settings.use_onvif_discovery:
        try:
            candidates = await discover_cameras(
                camera, profile=StreamProfile(settings.default_stream_profile))
        except OSError:
            logging.getLogger(__name__).warning("ONVIF discovery unavailable; using manual camera")
            candidates = []
        reachable = [candidate for candidate in candidates if candidate.rtsp_reachable]
        if len(reachable) == 1:
            camera = camera.model_copy(update={"host": reachable[0].host})
        elif len(reachable) > 1:
            raise RuntimeError(
                "Multiple reachable cameras discovered; run `mantau-agent setup` "
                "to select one explicitly"
            )
        elif not camera.host:
            raise RuntimeError(
                "No reachable ONVIF camera and no MANTAU_CAMERA_HOST fallback configured")
    return camera


async def build_pipeline(settings: Settings, *,
                         inference_uplink: InferenceUplink | None = None,
                         configuration_store=None) -> MonitoringPipeline:
    """Build the monitoring pipeline.

    Server inference is required. Unless an adapter is injected (tests),
    `GET /inference/capability` must advertise an available detector. Local
    detector settings are ignored and no local model is initialized.
    """
    if not settings.agent_id or not settings.agent_secret:
        raise SystemExit(
            "This agent is not enrolled -- run `mantau-agent setup --key <enrollment key>` "
            "with a key from the Mantau app (Beranda > Tambah perangkat)."
        )

    if not settings.cloud_inference_enabled:
        raise RuntimeError("Server inference is required; enable MANTAU_CLOUD_INFERENCE_ENABLED")
    settings.inference_mode = InferenceMode.CLOUD
    camera = await _discover_camera(settings)
    cloud_upload_fps = settings.cloud_upload_fps
    owned_uplink = None
    inference_probe = None
    if inference_uplink is None:
        capability = await discover_capability(settings.server_url)
        if capability is not None and capability.available:
            inference_uplink = owned_uplink = HttpInferenceUplink(
                settings.server_url, settings.agent_id, settings.agent_secret, capability)
            cloud_upload_fps = cloud_rate(settings.cloud_upload_fps, capability)
        else:
            logging.getLogger(__name__).warning("Server inference unavailable; capturing without protection until recovery")

            async def inference_probe() -> bool:
                offered = await discover_capability(settings.server_url)
                return offered is not None and offered.available
    capabilities = await asyncio.to_thread(
        inspect_capabilities, cloud_available=inference_uplink is not None)
    async with AsyncExitStack() as cleanup:
        if owned_uplink is not None:
            cleanup.push_async_callback(owned_uplink.close)
        spool = EnvelopeSpool(DurableSpool(settings.spool_path, ttl_s=settings.spool_ttl_s))
        cleanup.callback(spool.close)
        uplink = UplinkClient(settings.server_url, settings.agent_id, settings.agent_secret,
                              SeqCounter(settings.seq_path), spool)
        cleanup.push_async_callback(uplink.close)
        frames = FrameUplink(
            settings.server_url, settings.agent_id, settings.agent_secret, settings.camera_id,
            fps=settings.live_view_fps, idle_fps=settings.live_view_idle_fps,
            jpeg_quality=settings.live_view_jpeg_quality,
            max_width=settings.live_view_max_width,
        )
        cleanup.push_async_callback(frames.close)
        stored = configuration_store.load() if configuration_store is not None else None
        activity = ActivityEngine(
            rules=build_activity_rules(),
            settings=(stored.detection_settings if stored is not None else None),
        )
        clips = None
        if settings.clips_enabled:
            clips = ClipRecorder(
                uploader=ClipUploader(settings.server_url, settings.agent_id,
                                      settings.agent_secret),
                encode_jpeg=frames.encode, spool_dir=Path(settings.clip_spool_dir).expanduser().resolve() / settings.agent_id,
                fps=settings.clip_fps, pre_s=settings.clip_pre_s, post_s=settings.clip_post_s,
                max_pending_files=settings.clip_max_kept, retain_local=True,
            )
            clips.set_enabled(activity.settings.recordings.enabled)
            if owned_uplink is not None:
                # Events the server detects get a review clip.
                owned_uplink.on_events = lambda events: [clips.on_event(e) for e in events]
        router = InferenceRouter(
            camera_id=settings.camera_id, activity=activity, clips=clips,
            frame_uplink=frames, capabilities=capabilities, mode=settings.inference_mode,
            inference_uplink=inference_uplink,
            sampler=FrameSampler(keep_every_n=settings.sampler_keep_every_n,
                                 max_fps=settings.sampler_max_fps),
            cloud_upload_fps=cloud_upload_fps,
            live_view_fps=settings.live_view_fps, live_view_enabled=settings.live_view_enabled,
            queue_size=settings.frame_queue_size, upload_timeout_s=settings.upload_timeout_s,
        )
        pipeline = MonitoringPipeline(
            puller=CameraPuller(camera, profile=StreamProfile(settings.default_stream_profile),
                                backoff=BackoffPolicy(base=settings.backoff_base_s,
                                                      cap=settings.backoff_cap_s)),
            router=router, uplink=uplink, spool=spool, tunnel=_build_tunnel(settings),
            agent_id=settings.agent_id, camera_id=settings.camera_id,
            poll_interval_s=settings.poll_interval_s,
            heartbeat_interval_s=settings.heartbeat_interval_s,
            spool_backoff=BackoffPolicy(base=settings.spool_retry_base_s,
                                        cap=settings.spool_retry_cap_s),
            status_store=StatusStore(settings.status_path),
            status_interval_s=settings.status_interval_s,
            inference_probe=inference_probe,
            inference_probe_interval_s=settings.inference_probe_interval_s,
        )
        if settings.command_channel_enabled:
            executor = CommandExecutor(
                settings, pipeline, config_store=configuration_store,
                restart_requested=pipeline.restart_requested.set)
            pipeline.control_worker = ControlPlaneWorker(
                settings.server_url, settings.agent_id, settings.agent_secret, executor,
                CompletedCommandStore(settings.command_state_path, secret=settings.agent_secret),
                poll_interval_s=settings.command_poll_interval_s,
            )
        cleanup.pop_all()
    logging.getLogger(__name__).info("Agent capabilities: %s", capabilities.model_dump_json())
    logging.getLogger(__name__).warning("Inference routing: %s", router.health())
    return pipeline


async def run(settings: Settings | None = None, *, config_path=None) -> None:
    if settings is None:
        settings, configuration = load_settings()
        from .setup_wizard import needs_setup
        if needs_setup(settings, configuration):
            raise RuntimeError(
                "Setup is incomplete; run `mantau-agent setup` or provide MANTAU_* variables")
    from .state import ConfigurationStore
    pipeline = await build_pipeline(settings, configuration_store=ConfigurationStore(config_path))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows' event loop doesn't support this for these signals

    try:
        await pipeline.start()
        waiters = (asyncio.create_task(stop_event.wait()),
                   asyncio.create_task(pipeline.restart_requested.wait()))
        _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await pipeline.shutdown()


async def _discover_command(settings: Settings) -> list[dict]:
    camera = _build_camera(settings)
    candidates = await discover_cameras(
        camera, profile=StreamProfile(settings.default_stream_profile))
    return [candidate.model_dump(mode="json") for candidate in candidates]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mantau-agent")
    parser.add_argument("--config", help="durable configuration path")
    parser.add_argument("--status-path", help="local health snapshot path")
    parser.add_argument("--server-url", help="override the configured server URL for this invocation")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run the monitoring service")
    setup = commands.add_parser("setup", help="enroll and select a validated camera")
    setup.add_argument("--remote", action="store_true", help="enroll now; configure camera from Mantau app")
    setup.add_argument("--key", dest="enrollment_key",
                       help="enrollment key from the Mantau app (Beranda > Tambah perangkat)")
    setup.add_argument("--name", help="device name shown in the Mantau app")
    setup.add_argument("--restore-backup", action="store_true",
                       help="restore the last known-good configuration before setup")
    status = commands.add_parser("status", help="show the last local health snapshot")
    status.add_argument("--json", action="store_true", dest="as_json")
    discovery = commands.add_parser("discover", help="discover and probe ONVIF cameras")
    discovery.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_value(value, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _normalize_local_status(status: dict) -> dict:
    value = dict(status)
    pid = value.get("pid")
    if value.get("running") and (not isinstance(pid, int) or not _process_exists(pid)):
        value["running"] = False
        value["status_warning"] = "Service process is no longer running"
        if isinstance(value.get("uplink"), dict):
            value["uplink"] = {**value["uplink"], "connected": False}
        if isinstance(value.get("camera"), dict):
            value["camera"] = {**value["camera"], "connected": False}
    return value


def cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"
    try:
        if command == "setup" and args.restore_backup:
            from .state import ConfigurationStore
            store = ConfigurationStore(args.config)
            store.restore_backup()
            print(f"Restored configuration from {store.backup_path}.")
            return 0
        settings, configuration = load_settings(args.config)
        if args.status_path:
            settings.status_path = args.status_path
        if args.server_url:
            settings.server_url = args.server_url
        logging.basicConfig(level=settings.log_level)
        if command == "setup":
            from .setup_wizard import run_wizard
            from .state import ConfigurationStore
            store = ConfigurationStore(args.config)
            run_wizard(store, remote=args.remote, server_url=args.server_url,
                       enrollment_key=args.enrollment_key, name=args.name)
            return 0
        if command == "status":
            status = StatusStore(settings.status_path).read() or {
                "running": False,
                "configured": bool(configuration and configuration.setup_state.value == "complete"),
                "version": __version__,
                "status_error": "No status snapshot has been written",
            }
            _print_value(_normalize_local_status(status), as_json=args.as_json)
            return 0
        if command == "discover":
            candidates = asyncio.run(_discover_command(settings))
            _print_value(candidates, as_json=args.as_json)
            return 0
        from .setup_wizard import enroll_and_save, needs_setup
        if (settings.enrollment_key and not settings.agent_secret
                and (configuration is None or not configuration.enrollment.agent_secret)):
            # Unattended first start (Docker): enroll, then wait for camera
            # setup from the app. The key is single-use, so this runs once.
            from .state import ConfigurationStore
            enroll_and_save(ConfigurationStore(args.config), settings.server_url,
                            settings.enrollment_key, name=settings.device_name or None)
            settings, configuration = load_settings(args.config)
            if args.status_path:
                settings.status_path = args.status_path
            if args.server_url:
                settings.server_url = args.server_url
        if needs_setup(settings, configuration):
            raise RuntimeError(
                "Setup is incomplete; run `mantau-agent setup` or set MANTAU_ENROLLMENT_KEY")
        asyncio.run(run(settings, config_path=args.config))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        # Deliberately never include settings/config representations here: both
        # contain secrets. Exception messages in our boundaries are sanitized.
        print(f"mantau-agent: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(cli())


if __name__ == "__main__":
    main()
