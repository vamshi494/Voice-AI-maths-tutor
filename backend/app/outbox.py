# app/outbox.py
"""Outbox: the server's event ring buffer and (epoch, seq) stamping, used when FEATURE_OUTBOX is on.

Every server event goes out through one Outbox. Events that were sent (or failed to send)
stay buffered so a reconnecting client can be resynced in order; the buffer evicts oldest
entries beyond OUTBOX_CAPACITY or OUTBOX_MAX_BYTES.
"""
from collections import deque
from uuid import uuid4

from app.config import settings
from app.observability import log_event, logger
from app.transport import PACKET_LIMIT_BYTES, _resolve_room


class Outbox:
    def __init__(self, capacity: int | None = None, max_bytes: int | None = None) -> None:
        self.epoch = uuid4().hex[:8]
        self.seq = 0
        self.capacity = capacity if capacity is not None else settings.OUTBOX_CAPACITY
        self.max_bytes = max_bytes if max_bytes is not None else settings.OUTBOX_MAX_BYTES
        self.buf: deque[tuple[int, str]] = deque()
        self._bytes = 0

    async def send(self, evt: object, room: object = None) -> str:
        """Stamp epoch/seq, store, and route by size (≤ 14 KiB packet, else text stream)."""
        self.seq += 1
        try:
            evt = evt.model_copy(update={"seq": self.seq, "epoch": self.epoch})
        except Exception:
            pass
        payload = evt.model_dump_json(by_alias=True, exclude_none=True)
        r = _resolve_room(room)
        if not (r and getattr(r, "local_participant", None)):
            log_event("transport_send_event_offline", event_type=type(evt).__name__)
            return "offline"
        data = payload.encode("utf-8")
        path = "failed"
        if len(data) <= PACKET_LIMIT_BYTES:
            try:
                await r.local_participant.publish_data(data, topic="tutor.events", reliable=True)
                path = "packet"
            except Exception as e:
                logger.warning(f"publish_data failed for {type(evt).__name__} ({len(data)} B): {e}")
        if path != "packet":
            try:
                await r.local_participant.send_text(payload, topic="tutor.events")
                path = "stream"
                log_event("transport_large_event_streamed", event_type=type(evt).__name__, bytes=len(data))
            except Exception as e:
                logger.error(f"Failed to publish event {type(evt).__name__}: {e}")
                log_event("transport_send_failed", event_type=type(evt).__name__, bytes=len(data), error=str(e))
        self._store(self.seq, payload)
        return path

    def _store(self, seq: int, payload: str) -> None:
        self.buf.append((seq, payload))
        self._bytes += len(payload.encode("utf-8"))
        while len(self.buf) > self.capacity or (self._bytes > self.max_bytes and len(self.buf) > 1):
            _seq, old = self.buf.popleft()
            self._bytes -= len(old.encode("utf-8"))

    async def resend_from(self, first_seq: int, room: object = None) -> bool:
        """Resend every buffered event with seq >= first_seq, in order. False when a gap
        (an evicted event) makes that impossible, so the caller falls back to a snapshot."""
        r = _resolve_room(room)
        if first_seq <= 0:
            first_seq = self.buf[0][0] if self.buf else self.seq + 1
        if self.buf and first_seq < self.buf[0][0]:
            return False                                    # the buffer does not start here
        if first_seq > self.seq:
            return True                                     # nothing to resend
        pending = [(s, p) for s, p in self.buf if s >= first_seq]
        if len(pending) != self.seq - first_seq + 1:
            return False
        if not (r and getattr(r, "local_participant", None)):
            return True
        for _seq, payload in pending:
            data = payload.encode("utf-8")
            try:
                if len(data) <= PACKET_LIMIT_BYTES:
                    await r.local_participant.publish_data(data, topic="tutor.events", reliable=True)
                else:
                    await r.local_participant.send_text(payload, topic="tutor.events")
            except Exception as e:
                log_event("transport_resend_failed", error=str(e))
                return False
        return True


_outbox: Outbox | None = None


def get_outbox() -> Outbox:
    global _outbox
    if _outbox is None:
        _outbox = Outbox()
    return _outbox


def reset_outbox() -> Outbox:
    global _outbox
    _outbox = Outbox()
    return _outbox


async def send_event(evt: object, room: object = None) -> str:
    return await get_outbox().send(evt, room)
