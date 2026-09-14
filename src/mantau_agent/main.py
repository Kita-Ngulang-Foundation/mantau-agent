"""Wires camera -> puller -> detect runner -> uplink client + heartbeat loop
into one running agent process. `_build_camera`/`_build_detector`/
`_build_tunnel` are plain factory functions and are tested directly; `run()`
is integration glue (real signal handling, an indefinite loop) exercised by
actually running the agent, not by the unit test suite -- the same honesty
line drawn around mantau-backend-rtsp's own `docker compose up` path.
"""

from __future__ import annotations

import asyncio
import signal

from mantau_core.buffer import DurableSpool
from mantau_core.contracts import CameraRef, Credentials, StreamProfile
from mantau_core.detection import Detector, NullDetector
from mantau_core.resilience import BackoffPolicy, Supervisor

from .camera.puller import CameraPuller
from .config import Settings
from .detect.runner import DetectRunner
from .detect.sampler import FrameSampler
from .health.heartbeat import HeartbeatLoop
from .uplink.client import UplinkClient
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
    return NullDetector(settings.camera_id, trigger_every_n_frames=settings.null_detector_trigger_every)


def _build_tunnel(settings: Settings) -> TunnelProvider:
    if settings.tunnel_provider == "tailscale":
        return TailscaleTunnel()
    return NullTunnel()


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    if not settings.agent_id or not settings.agent_secret:
        raise SystemExit(
            "MANTAU_AGENT_ID and MANTAU_AGENT_SECRET must be set -- enroll this agent "
            "against the server first (POST /agents/enroll) and copy the secret it returns."
        )

    camera = _build_camera(settings)
    tunnel = _build_tunnel(settings)
    await tunnel.up()

    seq_counter = SeqCounter(settings.seq_path)
    spool = EnvelopeSpool(DurableSpool(settings.spool_path, ttl_s=settings.spool_ttl_s))
    uplink = UplinkClient(settings.server_url, settings.agent_id, settings.agent_secret,
                          seq_counter, spool)

    puller = CameraPuller(
        camera, profile=StreamProfile(settings.default_stream_profile),
        backoff=BackoffPolicy(base=settings.backoff_base_s, cap=settings.backoff_cap_s),
    )
    puller.start()

    detector = _build_detector(settings)
    sampler = FrameSampler(keep_every_n=settings.sampler_keep_every_n, max_fps=settings.sampler_max_fps)

    async def on_event(event):
        await uplink.send_event(event)

    runner = DetectRunner(puller, detector, on_event, sampler=sampler,
                          poll_interval_s=settings.poll_interval_s)
    heartbeat = HeartbeatLoop(
        settings.agent_id, settings.camera_id, uplink,
        interval_s=settings.heartbeat_interval_s,
        camera_reachable=lambda: puller.reachable,
        queue_depth=spool.depth,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows' event loop doesn't support this for these signals

    detect_supervisor = Supervisor("detect-runner", lambda: runner.run(stop_event=stop_event),
                                   backoff=BackoffPolicy(base=1.0, cap=15.0))
    heartbeat_supervisor = Supervisor("heartbeat", lambda: heartbeat.run(stop_event=stop_event),
                                      backoff=BackoffPolicy(base=1.0, cap=15.0))

    try:
        await asyncio.gather(
            detect_supervisor.run_forever(stop_event=stop_event),
            heartbeat_supervisor.run_forever(stop_event=stop_event),
        )
    finally:
        puller.stop()
        await uplink.close()
        await tunnel.down()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
