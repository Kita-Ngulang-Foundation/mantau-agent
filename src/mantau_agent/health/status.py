"""Persistent, secret-free local service status for the CLI."""

from __future__ import annotations

import json
from pathlib import Path

from ..storage import atomic_write_json


class StatusStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, status: dict) -> None:
        atomic_write_json(self.path, status, mode=0o644)

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"running": False, "status_error": f"Unreadable status file: {type(exc).__name__}"}
        if not isinstance(value, dict):
            return {"running": False, "status_error": "Unreadable status file: expected object"}
        return value
