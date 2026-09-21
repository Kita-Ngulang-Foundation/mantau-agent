"""Build an envelope, try to send it, spool it on failure, drain the spool
whenever a send succeeds. Never raises past this module's boundary --
`detect/runner.py`'s `on_event` and the heartbeat loop fire-and-forget into
`send_event`/`send_heartbeat`; a fall event that can't reach the server yet
must never crash the detection loop that found it.
"""

from __future__ import annotations

import asyncio

import httpx
from mantau_core.contracts import Envelope, FallEvent, Heartbeat

from .seq import SeqCounter
from .spool import EnvelopeSpool


class UplinkClient:
    def __init__(
        self,
        server_url: str,
        agent_id: str,
        secret: str,
        seq_counter: SeqCounter,
        spool: EnvelopeSpool,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self._secret = secret
        self._seq = seq_counter
        self._spool = spool
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def send_event(self, event: FallEvent) -> None:
        envelope = Envelope.for_event(self.agent_id, self._seq.next(), event).sign(self._secret)
        await self._send_or_spool(envelope)

    async def send_heartbeat(self, heartbeat: Heartbeat) -> None:
        envelope = Envelope.for_heartbeat(self.agent_id, self._seq.next(), heartbeat).sign(self._secret)
        await self._send_or_spool(envelope)

    async def _send_or_spool(self, envelope: Envelope) -> None:
        self._spool.evict_expired()
        try:
            await self._post(envelope)
        except asyncio.CancelledError:
            self._spool.put(envelope)
            raise
        except httpx.HTTPError:
            self._spool.put(envelope)
            return
        # A send just succeeded -- also a good moment to clear anything that
        # piled up during a prior outage, without waiting for a new event.
        await self.drain_spool()

    async def _post(self, envelope: Envelope) -> None:
        resp = await self._client.post(
            f"{self.server_url}/ingest", json=envelope.model_dump(mode="json")
        )
        resp.raise_for_status()

    async def drain_spool(self, *, max_items: int = 50) -> int:
        """Attempt to send everything spooled, oldest first. Stops at the
        first failure (the tunnel is presumably still down) instead of
        burning through every item's retry on every call."""
        self._spool.evict_expired()
        sent = 0
        for envelope in self._spool.pending(limit=max_items):
            try:
                await self._post(envelope)
            except httpx.HTTPError:
                self._spool.mark_attempted(envelope)
                break
            else:
                self._spool.ack(envelope)
                sent += 1
        return sent

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
