"""Frame decimation -- the target hardware (Orange Pi Zero 2W class, ~Rp350k)
cannot run detection at full camera framerate the way a laptop can.

Two independent, composable knobs -- usually you'd use just one:
- `keep_every_n`: skip N-1 out of every N frames. Simple, deterministic.
- `max_fps`: keep a frame only if enough wall-clock time passed since the
  last kept one. More robust to a camera whose actual frame rate drifts
  from what it claims.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FrameSampler:
    keep_every_n: int = 1          # 1 = never skip on this axis
    max_fps: float | None = None   # None = no time-based throttle

    def __post_init__(self) -> None:
        self._frame_count = 0
        self._last_kept_ts_ms: int | None = None

    def should_keep(self, ts_ms: int) -> bool:
        self._frame_count += 1
        if self.keep_every_n > 1 and self._frame_count % self.keep_every_n != 0:
            return False
        if self.max_fps is not None and self._last_kept_ts_ms is not None:
            min_interval_ms = 1000.0 / self.max_fps
            if ts_ms - self._last_kept_ts_ms < min_interval_ms:
                return False
        self._last_kept_ts_ms = ts_ms
        return True
