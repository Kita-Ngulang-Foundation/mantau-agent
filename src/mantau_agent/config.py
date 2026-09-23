"""Agent-specific settings, extending mantau_core's shared base. Same
single `MANTAU_`-prefixed namespace as both server-side backends.

`spool_ttl_s` (inherited from `CoreSettings`, default 300s) is the agent's
own "hold recent events through a brief outage" window from the brief --
reused directly, not redeclared, since it means the same thing here as it
does anywhere else `mantau_core.buffer.DurableSpool` is used.
"""

from __future__ import annotations

from mantau_core.config import CoreSettings
from pydantic import Field
from pydantic_settings import PydanticBaseSettingsSource

from .capabilities import InferenceMode


class Settings(CoreSettings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Durable configuration is passed as init data. Explicit environment
        # and legacy .env values must remain able to override it for Docker/CI.
        return env_settings, dotenv_settings, init_settings, file_secret_settings

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
    # Directory holding the pinned model files (see mantau_core.detection.
    # artifacts); empty = the models packaged with the installed mantau-AI.
    model_dir: str = ""
    fall_classifier_enabled: bool = True
    inference_mode: InferenceMode = InferenceMode.AUTO
    # 15 fps matched full-frame-rate accuracy on the UR Fall and Y-B-Class clips;
    # at 5-10 fps the tracker loses the person mid-fall and recall drops.
    detection_fps: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    # CLOUD sends frames for the server to run the fall detector on. Below
    # ~10 fps the fall tracker loses people mid-fall (mantau-AI
    # docs/EVALUATION.md); the server's advertised max_fps caps this.
    cloud_upload_fps: float = Field(default=10.0, gt=0, le=15.0, allow_inf_nan=False)
    # Use server inference when the server offers it (CLOUD/HYBRID and the
    # automatic fallback when the on-device detector cannot run).
    cloud_inference_enabled: bool = True
    hybrid_confirmation_fps: float = Field(default=0.2, gt=0, allow_inf_nan=False)
    frame_queue_size: int = Field(default=2, ge=1)
    upload_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # None = never fire a synthetic fall. Notifications are supposed to mean a
    # real fall happened, so the default detector stays silent until a real
    # model is wired in; set a frame count only when demoing the alert path.
    null_detector_trigger_every: int | None = None
    sampler_keep_every_n: int = Field(default=1, ge=1)
    sampler_max_fps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    poll_interval_s: float = Field(default=0.02, gt=0, allow_inf_nan=False)

    # -- live view ---------------------------------------------------------------
    # Review clips around events (never for bathroom-duration events).
    clips_enabled: bool = True
    clip_fps: float = Field(default=5.0, gt=0, le=15, allow_inf_nan=False)
    clip_pre_s: float = Field(default=5.0, ge=1, le=30)
    clip_post_s: float = Field(default=5.0, ge=1, le=30)
    clip_spool_dir: str = "data/clips"
    live_view_enabled: bool = True
    live_view_fps: float = Field(default=4.0, gt=0, allow_inf_nan=False)
    live_view_jpeg_quality: int = 70
    live_view_max_width: int = 640

    # -- uplink -----------------------------------------------------------------
    tunnel_provider: str = "null"  # "null" | "tailscale"
    seq_path: str = "data/seq.txt"
    spool_path: str = "data/spool.db"
    heartbeat_interval_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    spool_retry_base_s: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    spool_retry_cap_s: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    status_path: str = "data/status.json"
    status_interval_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    command_channel_enabled: bool = False
    command_poll_interval_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    command_state_path: str = "data/commands.json"


def load_settings(path=None) -> tuple[Settings, object | None]:
    """Load strict durable state, then apply compatible MANTAU_* overrides."""
    from .state import ConfigurationStore

    configuration = ConfigurationStore(path).load()
    data = configuration.settings_data() if configuration is not None else {}
    return Settings(**data), configuration
