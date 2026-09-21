"""Hold envelopes the server hasn't confirmed yet, through a tunnel outage.

Thin wrapper over `mantau_core.buffer.DurableSpool` -- that already does the
SQLite persistence and TTL eviction; this module only knows how to turn an
`Envelope` into the string `DurableSpool` stores, and back.
"""

from __future__ import annotations

from mantau_core.buffer import DurableSpool
from mantau_core.contracts import Envelope


class EnvelopeSpool:
    def __init__(self, spool: DurableSpool) -> None:
        self._spool = spool

    @staticmethod
    def _key(envelope: Envelope) -> str:
        return f"{envelope.agent_id}:{envelope.seq}"

    def put(self, envelope: Envelope) -> None:
        self._spool.put(self._key(envelope), envelope.model_dump_json())

    def pending(self, limit: int = 100) -> list[Envelope]:
        return [Envelope.model_validate_json(item.payload) for item in self._spool.pending(limit=limit)]

    def ack(self, envelope: Envelope) -> None:
        self._spool.ack(self._key(envelope))

    def mark_attempted(self, envelope: Envelope) -> None:
        self._spool.mark_attempted(self._key(envelope))

    def evict_expired(self) -> int:
        return self._spool.evict_expired()

    def depth(self) -> int:
        return self._spool.depth()

    def close(self) -> None:
        self._spool.close()
