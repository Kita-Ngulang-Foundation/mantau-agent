"""A persisted, monotonically increasing per-agent sequence counter.

`seq` must never repeat across an agent restart (see
`../../../protocol/PROTOCOL.md`) -- a plain text file, written atomically
(write-then-rename) rather than in-place, so a power loss mid-write can't
leave a corrupt half-written number behind. That's a real risk on the
target hardware (a small board with no UPS), not a theoretical one.
"""

from __future__ import annotations

from pathlib import Path

from ..storage import atomic_write_bytes


class CorruptSequenceError(RuntimeError):
    pass


class SeqCounter:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._value = self._read()

    def _read(self) -> int:
        if not self._path.exists():
            return 0
        try:
            value = int(self._path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError) as exc:
            raise CorruptSequenceError(
                f"Sequence state {self._path} is unreadable; refusing to reuse sequence numbers"
            ) from exc
        if value < 0:
            raise CorruptSequenceError(
                f"Sequence state {self._path} is negative; refusing to reuse sequence numbers")
        return value

    def next(self) -> int:
        """The next seq to use. Persists BEFORE returning, so a crash right
        after this call never risks reusing a seq that might already be in
        flight to the server."""
        value = self._value
        self._value += 1
        self._write(self._value)
        return value

    def _write(self, value: int) -> None:
        atomic_write_bytes(self._path, str(value).encode("ascii"), mode=0o600)
