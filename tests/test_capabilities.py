import json

import pytest

from mantau_agent.capabilities import (
    CapabilityReport, InferenceMode as Mode, PlatformType, classify_platform,
    inspect_capabilities, select_inference_mode,
)
from mantau_agent.config import Settings


def report(**overrides):
    return CapabilityReport(**{
        "platform": PlatformType.LINUX_ARM64, "architecture": "aarch64",
        "cpu": "test CPU", "cpu_count": 4, "memory_bytes": 1024**3,
        "software_version": "0.1.0", "detector_backend": "mediapipe",
        "supported_detector_backends": ["null", "mediapipe"], "detector_fps": 10,
        "cloud_available": True,
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


@pytest.mark.parametrize("changes,expected,reason", [
    ({}, Mode.EDGE, "Production detector"),
    ({"detector_fps": 4.9}, Mode.CLOUD, "throughput"),
    ({"detector_fps": None}, Mode.CLOUD, "throughput"),
    ({"memory_bytes": 256 * 1024**2}, Mode.CLOUD, "Memory"),
    ({"memory_bytes": None}, Mode.CLOUD, "Memory"),
    ({"detector_backend": "null"}, Mode.CLOUD, "synthetic"),
    ({"supported_detector_backends": ["null"]}, Mode.CLOUD, "production"),
    ({"detector_error": "unavailable"}, Mode.CLOUD, "production"),
    ({"memory_bytes": 512 * 1024**2, "detector_fps": 5}, Mode.EDGE, "Production"),
])
def test_auto_is_deterministic_and_conservative(changes, expected, reason):
    capabilities = report(**changes)
    first = select_inference_mode(Mode.AUTO, capabilities)
    assert first == select_inference_mode(Mode.AUTO, capabilities)
    assert first.mode == expected
    assert reason in first.reason


@pytest.mark.parametrize("mode", [Mode.EDGE, Mode.CLOUD, Mode.HYBRID])
def test_explicit_modes_preserve_operator_choice(mode):
    result = select_inference_mode(mode, report(detector_backend="null"))
    assert result.mode == mode
    assert result.reason == "Explicit operator selection"


def test_capability_report_json_roundtrip():
    original = report(available_accelerators=["opencv_cuda"])
    assert CapabilityReport.model_validate_json(original.model_dump_json()) == original
    assert json.loads(original.model_dump_json())["platform"] == "linux_arm64"


def test_probe_measures_initialized_detector_and_discards_outputs(monkeypatch):
    calls = []

    class Probe:
        def push(self, frame, ts):
            calls.append((frame.shape, ts))
            return [object()]

    monkeypatch.setattr("mantau_agent.capabilities._memory_bytes", lambda: 1024**3)
    result = inspect_capabilities(detector_backend="mediapipe", detector=Probe())
    assert len(calls) == 4
    assert result.detector_fps > 0
    assert "mediapipe" in result.supported_detector_backends


def test_failed_probe_does_not_claim_backend_support():
    class Broken:
        def push(self, frame, ts):
            raise RuntimeError("model broken")

    result = inspect_capabilities(detector_backend="mediapipe", detector=Broken())
    assert result.supported_detector_backends == ["null"]
    # Nothing can detect falls here: no EDGE, and CLOUD is not implemented.
    assert result.supported_inference_modes == [Mode.AUTO]
    assert result.recommended_mode == Mode.AUTO
    assert result.detector_error and result.recommendation_reason == result.detector_error


def test_benchmark_is_preferred_over_blank_frames(monkeypatch):
    class Real:
        def benchmark(self):
            return 23.5

        def push(self, frame, ts):
            raise AssertionError("blank-frame probe must not run when benchmark exists")

    monkeypatch.setattr("mantau_agent.capabilities._memory_bytes", lambda: 1024**3)
    result = inspect_capabilities(detector_backend="mediapipe", detector=Real())
    assert result.detector_fps == 23.5
    assert result.supported_inference_modes == [Mode.AUTO, Mode.EDGE]
    assert result.recommended_mode == Mode.EDGE


def test_benchmark_failure_means_no_edge():
    class ModelMissing:
        def benchmark(self):
            raise FileNotFoundError("pose model")

    result = inspect_capabilities(detector_backend="mediapipe", detector=ModelMissing())
    assert Mode.EDGE not in result.supported_inference_modes
    assert "FileNotFoundError" in result.detector_error


def test_cloud_is_never_claimed_without_a_transport():
    assert Mode.CLOUD not in report(cloud_available=False).supported_inference_modes
    assert Mode.HYBRID not in report(cloud_available=False).supported_inference_modes
    assert report().supported_inference_modes == [Mode.AUTO, Mode.EDGE, Mode.CLOUD, Mode.HYBRID]
    dumped = json.loads(report(cloud_available=False).model_dump_json())
    assert dumped["supported_inference_modes"] == ["AUTO", "EDGE"]


@pytest.mark.parametrize("changes,reason", [
    ({"detector_fps": 4.9}, "throughput"),
    ({"memory_bytes": None}, "Memory"),
])
def test_constrained_detector_still_runs_edge_when_cloud_is_unavailable(changes, reason):
    selection = select_inference_mode(Mode.AUTO, report(cloud_available=False, **changes))
    assert selection.mode == Mode.EDGE
    assert reason in selection.reason and "cloud inference is unavailable" in selection.reason


@pytest.mark.parametrize("setting,value", [
    ("detection_fps", 0), ("cloud_upload_fps", -1), ("hybrid_confirmation_fps", 0),
    ("live_view_fps", float("inf")), ("frame_queue_size", 0),
    ("sampler_keep_every_n", 0), ("sampler_max_fps", 0), ("poll_interval_s", 0),
])
def test_invalid_rates_and_bounds_rejected(setting, value):
    with pytest.raises(ValueError):
        Settings(**{setting: value})
