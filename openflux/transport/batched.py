"""BatchedTransport — coalescing + zstd, a port of Go's transport/batched.py.

Queues outgoing mux packets, coalesces bursts into a single framed+zstd batch
per inner transport message, and splits received batches back into packets.
Symmetric: client and exit both wrap their transport with this.

Env overrides (same names as Go): OPENFLUX_BATCH_BYTES, OPENFLUX_BATCH_COUNT,
OPENFLUX_BATCH_LINGER_MS.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from . import framing
from .base import ReceiveCallback, Transport, TransportStats
from .. import logging_util as log

# A batch is sized to fit, after encryption framing, inside one cups.online
# paced message (see cupsonline.max_message_data) so a mux segment is never
# split across two paced sends. ~9 KiB payload + crypto overhead stays < 11 KiB.
DEFAULT_MAX_BATCH_BYTES = 9000
DEFAULT_MAX_BATCH_COUNT = 64
DEFAULT_LINGER_MS = 5
BATCH_QUEUE_DEPTH = 1024


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v:
        try:
            n = int(v)
            if n > 0:
                return n
        except ValueError:
            pass
    return default


class BatchedTransport(Transport):
    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=BATCH_QUEUE_DEPTH)
        self._linger = _env_int("OPENFLUX_BATCH_LINGER_MS", DEFAULT_LINGER_MS) / 1000.0
        self._max_bytes = min(_env_int("OPENFLUX_BATCH_BYTES", DEFAULT_MAX_BATCH_BYTES),
                              framing.MAX_FRAME_BYTES - 65537)
        self._max_count = min(_env_int("OPENFLUX_BATCH_COUNT", DEFAULT_MAX_BATCH_COUNT),
                              framing.MAX_FRAME_RECORDS - 1)
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._user_cb: Optional[ReceiveCallback] = None
        self._send_errors = 0

    async def start(self) -> None:
        if self._running:
            return
        await self._inner.start()
        self._running = True
        self._flush_task = asyncio.ensure_future(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._inner.stop()

    async def send(self, data: bytes) -> None:
        if not self._running:
            raise RuntimeError("batched transport is not running")
        if len(data) > 65535:
            raise ValueError(f"packet too large for batch record: {len(data)} bytes")
        try:
            self._queue.put_nowait(bytes(data))
        except asyncio.QueueFull:
            raise RuntimeError("batch queue full")

    def set_receive(self, cb: ReceiveCallback) -> None:
        self._user_cb = cb

        def on_message(data: bytes) -> None:
            try:
                pkts = framing.decode_batch(data)
            except Exception as e:  # noqa: BLE001
                log.debugf("[BATCH] decode error (%d bytes): %s", len(data), e)
                return
            cb2 = self._user_cb
            if cb2 is None:
                return
            for p in pkts:
                cb2(p)

        self._inner.set_receive(on_message)

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def stats(self) -> TransportStats:
        return self._inner.stats()

    def send_errors(self) -> int:
        return self._send_errors

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return
            batch = [first]
            size = 2 + len(first)

            # Phase 1: drain everything already queued (burst coalescing).
            while size < self._max_bytes and len(batch) < self._max_count:
                try:
                    p = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                batch.append(p)
                size += 2 + len(p)

            # Phase 2: brief linger to catch stragglers.
            if self._linger > 0 and size < self._max_bytes and len(batch) < self._max_count:
                deadline = asyncio.get_event_loop().time() + self._linger
                while size < self._max_bytes and len(batch) < self._max_count:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        p = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    except asyncio.CancelledError:
                        return
                    batch.append(p)
                    size += 2 + len(p)

            try:
                await self._inner.send(framing.encode_batch(batch))
            except Exception as e:  # noqa: BLE001
                self._send_errors += 1
                log.debugf("[BATCH] send error: %s", e)
