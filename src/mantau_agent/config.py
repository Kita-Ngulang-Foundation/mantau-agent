"""Agent-specific settings, extending mantau_core's shared base. Same
single `MANTAU_`-prefixed namespace as both server-side backends.

`spool_ttl_s` (inherited from `CoreSettings`, default 300s) is the agent's
own "hold recent events through a brief outage" window from the brief --
reused directly, not redeclared, since it means the same thing here as it
does anywhere else `mantau_core.buffer.DurableSpool` is used.
"""

from __future__ import annotations

from mantau_core.config import CoreSettings


class Settings(CoreSettings):
    # -- server connection ---------------------------------------------------
    server_url: str = "http://localhost:8100"
    agent_id: str = ""      # set via MANTAU_AGENT_ID, from POST /agents/enroll
    agent_secret: str = ""  # set via MANTAU_AGENT_SECRET, from the same call

    # -- camera: manual config path (ONVIF discovery picks its own CameraRef) -
    camera_id: str = "cam-1"
    camera_host: str = ""
    camera_port: int = 554
    camera_main_path: str = "/stream1"
    camera_sub_path: str | None = None
    camera_username: str | None = None
    camera_password: str | None = None
    use_onvif_discovery: bool = False
    default_stream_profile: str = "sub"  # "sub" | "main"

    # -- detection -------------------------------------------------------------
    detector_backend: str = "null"       # "null" | "mediapipe"
    null_detector_trigger_every: int | None = 150
    sampler_keep_every_n: int = 1
    sampler_max_fps: float | None = None
    poll_interval_s: float = 0.02

    # -- uplink -----------------------------------------------------------------
    tunnel_provider: str = "null"  # "null" | "tailscale"
    seq_path: str = "data/seq.txt"
    spool_path: str = "data/spool.db"
    heartbeat_interval_s: float = 30.0
