"""Integration seam for server inference. There is no HTTP adapter yet.

The existing /cameras/{id}/frame endpoint is live-view storage ONLY. A future
adapter must define authentication, frame timestamps, event correlation,
idempotency, confirmation results and retention with mantau-core/server.
"""

from typing import Protocol


class InferenceUplink(Protocol):
    async def submit(self, jpeg: bytes, *, camera_id: str, ts_ms: int,
                     event_ids: tuple[str, ...] = ()) -> bool:
        """Accept a sampled frame; event_ids identifies HYBRID confirmation.

        True means accepted, not that inference/confirmation has completed.
        False or an exception is a failed upload; frames are disposable.
        """
        ...

    async def close(self) -> None: ...
