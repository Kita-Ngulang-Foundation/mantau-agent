"""Import only server-approved legacy clips; preserve originals for rollback."""
import asyncio
import json
import re
from pathlib import Path
from .storage import atomic_write_json


class LegacyClipMigrator:
    def __init__(self, legacy: Path, recorder):
        self.legacy = legacy.absolute()
        self.recorder = recorder
        self.cursor = 0
        self.next_attempt = 0.0

    def current(self):
        return not self.recorder._closed

    async def step(self):
        recorder = self.recorder
        if not self.current() or recorder._wall() < self.next_attempt or not self.legacy.exists():
            return
        self.next_attempt = recorder._wall() + 60
        if self.legacy.is_symlink() or self.legacy.resolve() != self.legacy:
            raise ValueError("unowned legacy directory")
        files = []
        for path in self.legacy.glob("*.mp4"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4", path.name):
                continue
            if path.is_symlink() or path.resolve().parent != self.legacy:
                raise ValueError("unowned legacy file")
            if 12 <= path.stat().st_size <= 20 * 1024 * 1024:
                files.append(path)
        files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        journal = recorder.spool_dir / "legacy-migration.json"
        recorder._owned(journal)
        if journal.exists() and journal.stat().st_size > 32 * 1024:
            raise ValueError("legacy journal too large")
        completed = json.loads(journal.read_text()) if journal.exists() else {}
        if not isinstance(completed, dict):
            raise ValueError("invalid migration journal")
        pending = [p for p in files[:128] if p.stem not in completed]
        if not pending:
            return
        start = self.cursor % len(pending)
        batch = (pending[start:] + pending[:start])[:5]
        self.cursor += len(batch)
        approved = await recorder.uploader.legacy_claims([p.stem for p in batch])
        for claim in approved:
            source = next((p for p in batch if p.stem == claim["event_id"]), None)
            if source is None or not self.current():
                continue
            digest = await asyncio.to_thread(recorder.import_legacy, source,
                claim["occurred_at_ms"] / 1000, self.current)
            if not self.current():
                return
            completed[source.stem] = digest
            recorder._owned(journal)
            atomic_write_json(journal, completed)
