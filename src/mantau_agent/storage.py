"""Small atomic JSON stores used for configuration and local health."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int = 0o600) -> None:
    """Durably replace ``path`` without exposing a partial or permissive file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, mode)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            os.chmod(target, mode)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: str | Path, value: Any, *, mode: int = 0o600) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    atomic_write_bytes(path, (payload + "\n").encode("utf-8"), mode=mode)
