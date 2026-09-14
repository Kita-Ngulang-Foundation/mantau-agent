"""A persisted, monotonically increasing per-agent sequence counter.

`seq` must never repeat across an agent restart (see
`../../../protocol/PROTOCOL.md`) -- a plain text file, written atomically
(write-then-rename) rather than in-place, so a power loss mid-write can't
leave a corrupt half-written number behind. That's a real risk on the
target hardware (a small board with no UPS), not a theoretical one.
"""

from __future__ import annotations

import os
from pathlib import Path


class SeqCounter:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._value = self._read()

    def _read(self) -> int:
        if not self._path.exists():
            return 0
        try:
            return int(self._path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return 0

    def next(self) -> int:
        """The next seq to use. Persists BEFORE returning, so a crash right
        after this call never risks reusing a seq that might already be in
        flight to the server."""
        value = self._value
        self._value += 1
        self._write(self._value)
        return value

    def _write(self, value: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(str(value), encoding="utf-8")
        os.replace(tmp, self._path)
