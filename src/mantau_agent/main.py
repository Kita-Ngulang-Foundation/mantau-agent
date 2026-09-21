"""Build the monitoring pipeline from existing camera, detector and uplinks."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from mantau_core.buffer import DurableSpool
from mantau_core.contracts import CameraRef, Credentials, StreamProfile
from mantau_core.detection import Detector, NullDetector
from mantau_core.resilience import BackoffPolicy

from .camera.puller import CameraPuller
from .capabilities import InferenceMode, inspect_capabilities
from .config import Settings
from .detect.router import InferenceRouter
from .detect.sampler import FrameSampler
from .discovery.onvif import discover
from .pipeline import MonitoringPipeline
from .uplink.client import UplinkClient
from .uplink.frames import FrameUplink
from .uplink.inference import InferenceUplink
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


def _build_detector(settings: Settings) -> Detector:
    if settings.detector_backend == "mediapipe":
        from mantau_core.detection.mediapipe_adapter import MediapipeDetector
        return MediapipeDetector(settings.camera_id, config={})
    if settings.detector_backend != "null":
        raise ValueError(f"Unknown detector backend: {settings.detector_backend}")
    return NullDetector(settings.camera_id, trigger_every_n_frames=settings.null_detector_trigger_every)


def _build_tunnel(settings: Settings) -> TunnelProvider:
    if settings.tunnel_provider == "tailscale":
        return TailscaleTunnel()
    return NullTunnel()


async def _discover_camera(settings: Settings) -> CameraRef:
    camera = _build_camera(settings)
    if settings.use_onvif_discovery:
        try:
            devices = await asyncio.to_thread(discover)
            host = next((device.host for device in devices if device.host), None)
            if host:
                camera = camera.model_copy(update={"host": host})
        except OSError:
            logging.getLogger(__name__).warning("ONVIF discovery unavailable; using manual camera")
    return camera


def _probe_capabilities(settings: Settings):
    probe = None
    error = None
    try:
        if settings.inference_mode != InferenceMode.CLOUD:
            try:
                probe = _build_detector(settings)
            except ImportError:
                if settings.inference_mode != InferenceMode.AUTO:
                    raise  # Preserve MediaPipe's actionable construction error.
                error = "Configured detector unavailable; install mantau-core[detection] streaming adapter"
        return inspect_capabilities(
            detector_backend=settings.detector_backend, detector=probe,
            detector_error=error, detection_fps=settings.detection_fps,
        )
    finally:
        if probe is not None:
            probe.close()


async def build_pipeline(settings: Settings, *,
                         inference_uplink: InferenceUplink | None = None) -> MonitoringPipeline:
    """Cloud adapter is injected until core/server publish an inference contract.

    Ownership of the injected adapter transfers to the returned pipeline only
    after successful construction.
    """
    if not settings.agent_id or not settings.agent_secret:
        raise SystemExit(
            "MANTAU_AGENT_ID and MANTAU_AGENT_SECRET must be set -- enroll this agent "
            "against the server first (POST /agents/enroll) and copy the secret it returns."
        )

    camera = await _discover_camera(settings)
    capabilities = await asyncio.to_thread(_probe_capabilities, settings)
    async with AsyncExitStack() as cleanup:
        detector = None
        if (settings.inference_mode != InferenceMode.CLOUD
                and settings.detector_backend in capabilities.supported_detector_backends):
            detector = await asyncio.to_thread(_build_detector, settings)
            cleanup.push_async_callback(asyncio.to_thread, detector.close)
        spool = EnvelopeSpool(DurableSpool(settings.spool_path, ttl_s=settings.spool_ttl_s))
        cleanup.callback(spool.close)
        uplink = UplinkClient(settings.server_url, settings.agent_id, settings.agent_secret,
                              SeqCounter(settings.seq_path), spool)
        cleanup.push_async_callback(uplink.close)
        frames = FrameUplink(
            settings.server_url, settings.agent_id, settings.agent_secret, settings.camera_id,
            fps=settings.live_view_fps, jpeg_quality=settings.live_view_jpeg_quality,
            max_width=settings.live_view_max_width,
        )
        cleanup.push_async_callback(frames.close)
        router = InferenceRouter(
            camera_id=settings.camera_id, detector=detector, event_uplink=uplink,
            frame_uplink=frames, capabilities=capabilities, mode=settings.inference_mode,
            inference_uplink=inference_uplink,
            sampler=FrameSampler(keep_every_n=settings.sampler_keep_every_n,
                                 max_fps=settings.sampler_max_fps),
            detection_fps=settings.detection_fps, cloud_upload_fps=settings.cloud_upload_fps,
            confirmation_fps=settings.hybrid_confirmation_fps,
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
        )
        cleanup.pop_all()
    logging.getLogger(__name__).info("Agent capabilities: %s", capabilities.model_dump_json())
    logging.getLogger(__name__).warning("Inference routing: %s", router.health())
    return pipeline


async def run(settings: Settings | None = None) -> None:
    pipeline = await build_pipeline(settings or Settings())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows' event loop doesn't support this for these signals

    try:
        await pipeline.start()
        await stop_event.wait()
    finally:
        await pipeline.shutdown()


def main() -> None:
    from .setup_wizard import needs_setup, run_wizard

    logging.basicConfig(level=Settings().log_level)
    if needs_setup():
        run_wizard()
    asyncio.run(run())


if __name__ == "__main__":
    main()
