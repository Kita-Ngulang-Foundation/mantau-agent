"""Local capability facts and a pure, deterministic inference policy.

Unknown measurements are conservative; installed packages or GPU device names
alone are never evidence that the configured detector can perform inference.
"""

from __future__ import annotations

import os
import platform
import time
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
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
    supported_detector_backends: list[str] = Field(default_factory=lambda: ["null"])
    software_version: str
    detector_backend: str = "null"
    detector_fps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    detector_error: str | None = None
    # CLOUD inference exists only when a cloud inference transport is wired in;
    # none ships yet, so every real agent reports False.
    cloud_available: bool = False
    recommended_mode: InferenceMode = InferenceMode.CLOUD
    recommendation_reason: str | None = None

    @property
    def detector_usable(self) -> bool:
        return (self.detector_backend != "null"
                and self.detector_backend in self.supported_detector_backends
                and not self.detector_error)

    @computed_field
    @property
    def supported_inference_modes(self) -> list[InferenceMode]:
        """Modes that can actually run here: EDGE needs a detector that loaded
        and ran; CLOUD needs a cloud transport; HYBRID needs both."""
        modes = [InferenceMode.AUTO]
        if self.detector_usable:
            modes.append(InferenceMode.EDGE)
        if self.cloud_available:
            modes.append(InferenceMode.CLOUD)
        if self.detector_usable and self.cloud_available:
            modes.append(InferenceMode.HYBRID)
        return modes


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


def select_inference_mode(requested: InferenceMode, report: CapabilityReport,
                          *, detection_fps: float = 5.0) -> ModeSelection:
    requested = InferenceMode(requested)
    if requested != InferenceMode.AUTO:
        return ModeSelection(mode=requested, reason="Explicit operator selection")
    if not report.detector_usable:
        suffix = "" if report.cloud_available else "; cloud inference is unavailable"
        return ModeSelection(mode=InferenceMode.CLOUD,
                             reason="No usable production detector; null is synthetic only" + suffix)
    constrained = None
    if report.memory_bytes is None or report.memory_bytes < 512 * 1024**2:
        constrained = "Memory unknown or below 512 MiB local-inference budget"
    elif report.detector_fps is None or report.detector_fps < detection_fps:
        constrained = "Measured detector throughput unknown or below requested rate"
    if constrained:
        if report.cloud_available:
            return ModeSelection(mode=InferenceMode.CLOUD, reason=constrained)
        # A slower local detector still detects falls; nothing else can.
        return ModeSelection(mode=InferenceMode.EDGE,
                             reason=constrained + "; cloud inference is unavailable, so EDGE")
    return ModeSelection(mode=InferenceMode.EDGE,
                         reason="Production detector measured at or above requested rate with sufficient memory")


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


def inspect_capabilities(*, detector_backend: str = "null", detector=None,
                         detector_error: str | None = None,
                         detection_fps: float = 5.0,
                         cloud_available: bool = False) -> CapabilityReport:
    """Probe host and optionally benchmark a disposable, initialized detector.

    Caller owns/ closes the probe detector; its outputs are discarded. A
    detector with `benchmark()` measures its real inference path (models loaded
    and run on a frame with a person); otherwise a small 320x240 blank-frame
    estimate is used. Either way it is a throughput figure, not an accuracy
    claim. Accelerators are reported only when OpenCV confirms a usable CUDA
    device.
    """
    measured_fps = None
    supported = ["null"]
    if detector is not None and detector_backend != "null":
        try:
            if hasattr(detector, "benchmark"):
                measured_fps = float(detector.benchmark())
            else:
                frame = np.zeros((240, 320, 3), dtype=np.uint8)
                detector.push(frame, 0)  # warm up, never uplink probe outputs
                start = time.perf_counter()
                for ts in (200, 400, 600):
                    detector.push(frame, ts)
                measured_fps = 3 / max(time.perf_counter() - start, 1e-9)
            if not measured_fps > 0:
                raise RuntimeError("benchmark measured no throughput")
            supported.append(detector_backend)
        except Exception as exc:
            detector_error = f"Detector probe failed: {type(exc).__name__}"
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
        available_accelerators=accelerators, supported_detector_backends=supported,
        software_version=software_version, detector_backend=detector_backend,
        detector_fps=measured_fps, detector_error=detector_error,
        cloud_available=cloud_available,
    )
    selection = select_inference_mode(InferenceMode.AUTO, report, detection_fps=detection_fps)
    # Never recommend a mode that cannot run here; AUTO then means "nothing
    # can detect falls on this agent yet", with the reason attached.
    supported_modes = report.supported_inference_modes
    report.recommended_mode = (selection.mode if selection.mode in supported_modes
                               else InferenceMode.AUTO)
    report.recommendation_reason = detector_error or selection.reason
    return report
