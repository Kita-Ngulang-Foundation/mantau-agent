import json

import pytest

from mantau_agent.capabilities import (
    CapabilityReport, InferenceMode as Mode, PlatformType, classify_platform,
    select_inference_mode,
)
from mantau_agent.config import Settings


def report(**overrides):
    return CapabilityReport(**{
        "platform": PlatformType.LINUX_ARM64, "architecture": "aarch64",
        "cpu": "test CPU", "cpu_count": 4, "memory_bytes": 1024**3,
        "software_version": "0.1.0", "cloud_available": True,
        **overrides,
    })


@pytest.mark.parametrize("system,arch,model,android,expected", [
    ("Linux", "x86_64", "", False, PlatformType.LINUX_X86_64),
    ("Linux", "aarch64", "", False, PlatformType.LINUX_ARM64),
    ("Linux", "armv7l", "Raspberry Pi 3", False, PlatformType.RASPBERRY_PI),
    ("Linux", "aarch64", "", True, PlatformType.ANDROID),
    ("Android", "arm64", "", False, PlatformType.ANDROID),
    ("Windows", "AMD64", "", False, PlatformType.OTHER),
])
def test_platform_identification(system, arch, model, android, expected):
    assert classify_platform(system, arch, model=model, android=android) == expected


@pytest.mark.parametrize("mode", [Mode.EDGE, Mode.CLOUD, Mode.HYBRID])
def test_explicit_modes_preserve_operator_choice(mode):
    result = select_inference_mode(mode, report())
    assert result.mode == Mode.CLOUD


def test_capability_report_json_roundtrip():
    original = report(available_accelerators=["opencv_cuda"])
    assert CapabilityReport.model_validate_json(original.model_dump_json()) == original
    assert json.loads(original.model_dump_json())["platform"] == "linux_arm64"


@pytest.mark.parametrize("setting,value", [
    ("cloud_upload_fps", -1),
    ("live_view_fps", float("inf")), ("frame_queue_size", 0),
    ("sampler_keep_every_n", 0), ("sampler_max_fps", 0), ("poll_interval_s", 0),
])
def test_invalid_rates_and_bounds_rejected(setting, value):
    with pytest.raises(ValueError):
        Settings(**{setting: value})
