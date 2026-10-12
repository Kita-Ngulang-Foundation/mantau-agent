"""Performance counters carried in each heartbeat's optional `stats` object.

Counters are DELTAS since the previous heartbeat that was built (not
acknowledged), so a spooled heartbeat carries its own interval. Gauges
(uptime, memory, load, CPU temperature) are sent as current values. Only
keys listed in `mantau_core.contracts.HEARTBEAT_STATS` are ever returned.
"""

from __future__ import annotations

import importlib.metadata
import math
import os
import time
from collections.abc import Callable

from mantau_core.contracts import HEARTBEAT_STATS

_STAT_CEILING = 999999.9
_PROC_STATUS = "/proc/self/status"
_CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"
_PROCESS_START = time.monotonic()


def _clean(value) -> float | None:
    """Round to one decimal, clamp to 0 <= value < 1_000_000, drop non-finite."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    number = 0.0 if number <= 0 else min(number, _STAT_CEILING)
    return round(number, 1)


def _memory_mb() -> float | None:
    try:
        with open(_PROC_STATUS, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def _load_1m() -> float | None:
    if not hasattr(os, "getloadavg"):
        return None
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def _cpu_temp_c() -> float | None:
    try:
        with open(_CPU_TEMP_PATH, encoding="utf-8") as handle:
            return int(handle.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


class HeartbeatStatsCollector:
    """Returns the stats for one heartbeat and resets the delta baseline."""

    def __init__(
        self,
        *,
        puller,
        router,
        inference_uplink_getter: Callable[[], object] | None = None,
        started_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._puller = puller
        self._router = router
        self._inference_uplink_getter = inference_uplink_getter
        self._clock = clock
        self._started = started_monotonic if started_monotonic is not None else _PROCESS_START
        self._previous: dict[str, float] = {}

    def _inference_uplink(self):
        if self._inference_uplink_getter is not None:
            return self._inference_uplink_getter()
        return getattr(self._router, "inference_uplink", None)

    def _cumulative(self) -> dict[str, float]:
        """Cumulative counters since process start; a key is absent if its source is."""
        snapshot: dict[str, float] = {}

        decoded = getattr(self._puller, "frames_decoded", None)
        if decoded is not None:
            snapshot["frames_decoded"] = float(decoded)

        reconnects = getattr(self._puller, "restarts", None)
        if reconnects is not None:
            snapshot["rtsp_reconnects"] = float(reconnects)

        down_seconds = getattr(self._puller, "camera_down_seconds", None)
        if callable(down_seconds):
            snapshot["camera_down_s"] = float(down_seconds())

        dropped = getattr(self._router, "dropped", None)
        if isinstance(dropped, dict):
            snapshot["frames_dropped"] = float(sum(dropped.values()))

        failures = getattr(self._router, "upload_failures", None)
        if isinstance(failures, dict):
            snapshot["upload_failures"] = float(sum(failures.values()))

        uplink = self._inference_uplink()
        if uplink is not None:
            uplink_stats = getattr(uplink, "stats", None)
            if isinstance(uplink_stats, dict) and "accepted" in uplink_stats:
                snapshot["frames_uploaded"] = float(uplink_stats["accepted"])

        return snapshot

    def __call__(self) -> dict[str, float]:
        raw: dict[str, float] = {}

        cumulative = self._cumulative()
        for key, current in cumulative.items():
            previous = self._previous.get(key, 0.0)
            delta = current - previous
            if delta < 0:
                # The counter went backwards: its source was reset, so the new
                # cumulative value is the interval's count.
                delta = current
            raw[key] = delta
            self._previous[key] = current

        raw["process_uptime_s"] = self._clock() - self._started
        memory = _memory_mb()
        if memory is not None:
            raw["memory_mb"] = memory
        load = _load_1m()
        if load is not None:
            raw["load_1m"] = load
        cpu_temp = _cpu_temp_c()
        if cpu_temp is not None:
            raw["cpu_temp_c"] = cpu_temp

        result: dict[str, float] = {}
        for key, value in raw.items():
            if key not in HEARTBEAT_STATS:
                continue
            cleaned = _clean(value)
            if cleaned is not None:
                result[key] = cleaned
        return result


def agent_version() -> str:
    try:
        return importlib.metadata.version("mantau-agent")
    except Exception:  # noqa: BLE001 -- the version must never stop the pipeline from starting
        from .. import __version__

        return __version__
