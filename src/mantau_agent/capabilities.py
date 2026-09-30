"""Local capability facts and the CLOUD-only inference policy.

Agents never detect on device: they upload sampled frames and the server runs
the fall detector. `InferenceMode` keeps all four values because agents
persist their mode (AUTO by default) and must still load an older stored
EDGE or HYBRID; every stored or requested mode resolves to CLOUD.
"""

from __future__ import annotations

import os
import platform
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, Field, computed_field


class PlatformType(str, Enum):
    LINUX_X86_64 = "linux_x86_64"
    LINUX_ARM64 = "linux_arm64"
    RASPBERRY_PI = "raspberry_pi"
    ANDROID = "android"  # Identification only; no Android runtime in this package.
    OTHER = "other"  # Keeps Windows development working.


class InferenceMode(str, Enum):
    EDGE = "EDGE"
    CLOUD = "CLOUD"
    HYBRID = "HYBRID"
    AUTO = "AUTO"


class ModeSelection(BaseModel):
    mode: InferenceMode
    reason: str


class CapabilityReport(BaseModel):
    platform: PlatformType
    architecture: str
    cpu: str
    cpu_count: int = Field(ge=1)
    memory_bytes: int | None = Field(default=None, ge=0)
    available_accelerators: list[str] = Field(default_factory=list)
    software_version: str
    # True only when the server offered inference at startup
    # (`GET /inference/capability`); without it the agent detects nothing.
    cloud_available: bool = False
    recommended_mode: InferenceMode = InferenceMode.CLOUD
    recommendation_reason: str | None = None

    @computed_field
    @property
    def supported_inference_modes(self) -> list[InferenceMode]:
        """AUTO and CLOUD, which both run as CLOUD. EDGE and HYBRID are still
        accepted from storage or a command, and also run as CLOUD."""
        return [InferenceMode.AUTO, InferenceMode.CLOUD]


def classify_platform(system: str, architecture: str, *, model: str = "",
                      android: bool = False) -> PlatformType:
    if android or system.lower() == "android":
        return PlatformType.ANDROID
    if system.lower() == "linux":
        if "raspberry pi" in model.lower():
            return PlatformType.RASPBERRY_PI
        if architecture.lower() in ("x86_64", "amd64"):
            return PlatformType.LINUX_X86_64
        if architecture.lower() in ("aarch64", "arm64"):
            return PlatformType.LINUX_ARM64
    return PlatformType.OTHER


SERVER_INFERENCE_UNAVAILABLE = "server inference unavailable; detecting nothing until it is back"


def select_inference_mode(requested: InferenceMode, report: CapabilityReport) -> ModeSelection:
    """Every mode resolves to CLOUD. There is no local fallback: without
    server inference the reason says so and health reports degraded."""
    requested = InferenceMode(requested)
    if not report.cloud_available:
        return ModeSelection(mode=InferenceMode.CLOUD, reason=SERVER_INFERENCE_UNAVAILABLE)
    if requested in (InferenceMode.AUTO, InferenceMode.CLOUD):
        return ModeSelection(mode=InferenceMode.CLOUD, reason="Server inference (CLOUD)")
    return ModeSelection(mode=InferenceMode.CLOUD,
                         reason=f"{requested.value} requested; this agent runs CLOUD only")


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return ""


def _memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def inspect_capabilities(*, cloud_available: bool = False) -> CapabilityReport:
    """Probe host facts. Accelerators are reported only when OpenCV confirms
    a usable CUDA device; nothing here loads or runs a model."""
    accelerators = []
    try:
        import cv2
        if cv2.cuda.getCudaEnabledDeviceCount() > 0:
            accelerators.append("opencv_cuda")
    except (ImportError, AttributeError, RuntimeError):
        pass
    try:
        software_version = version("mantau-agent")
    except PackageNotFoundError:
        software_version = "0.1.0"
    arch = platform.machine().lower()
    report = CapabilityReport(
        platform=classify_platform(platform.system(), arch,
                                   model=_read("/proc/device-tree/model"),
                                   android=bool(os.environ.get("ANDROID_ROOT"))),
        architecture=arch, cpu=platform.processor() or arch,
        cpu_count=os.cpu_count() or 1, memory_bytes=_memory_bytes(),
        available_accelerators=accelerators, software_version=software_version,
        cloud_available=cloud_available,
    )
    selection = select_inference_mode(InferenceMode.AUTO, report)
    report.recommended_mode = selection.mode
    report.recommendation_reason = selection.reason
    return report
