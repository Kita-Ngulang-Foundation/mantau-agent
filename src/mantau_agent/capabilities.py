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
from pydantic import BaseModel, Field


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
    recommended_mode: InferenceMode = InferenceMode.CLOUD


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
    if (report.detector_backend == "null"
            or report.detector_backend not in report.supported_detector_backends
            or report.detector_error):
        return ModeSelection(mode=InferenceMode.CLOUD,
                             reason="No usable production detector; null is synthetic only")
    if report.memory_bytes is None or report.memory_bytes < 512 * 1024**2:
        return ModeSelection(mode=InferenceMode.CLOUD,
                             reason="Memory unknown or below 512 MiB local-inference budget")
    if report.detector_fps is None or report.detector_fps < detection_fps:
        return ModeSelection(mode=InferenceMode.CLOUD,
                             reason="Measured detector throughput unknown or below requested rate")
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
                         detection_fps: float = 5.0) -> CapabilityReport:
    """Probe host and optionally benchmark a disposable, initialized detector.

    Caller owns/ closes the probe detector; its outputs are discarded. The small
    320x240 blank-frame benchmark is a startup estimate, not an accuracy claim.
    Accelerators are reported only when OpenCV confirms a usable CUDA device.
    """
    measured_fps = None
    supported = ["null"]
    if detector is not None and detector_backend != "null":
        try:
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
            detector.push(frame, 0)  # warm up, never uplink probe outputs
            start = time.perf_counter()
            for ts in (200, 400, 600):
                detector.push(frame, ts)
            measured_fps = 3 / max(time.perf_counter() - start, 1e-9)
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
    )
    report.recommended_mode = select_inference_mode(
        InferenceMode.AUTO, report, detection_fps=detection_fps).mode
    return report
