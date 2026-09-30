"""DirectTransport — plain TCP carrier, a port of transport/direct.go.

Carries already-framed messages over one TCP connection, each as
``[2-byte big-endian length][payload]``. Client mode dials and reconnects with
backoff; exit mode listens and serves one active peer at a time. Meant to be
wrapped by EncryptedTransport; the CLI refuses ``direct`` without a key.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .base import BaseTransport, TransportConfig
from .. import logging_util as log


@dataclass
class DirectConfig:
    listen_addr: str = ""
    dial_addr: str = ""
    is_exit: bool = False
    handshake_timeout: float = 15.0
    keepalive_interval: float = 30.0
    reconnect_min_delay: float = 0.2
    reconnect_max_delay: float = 15.0
    reconnect_multiplier: float = 1.4
    max_record_bytes: int = 65535


def _split_hostport(addr: str, default_host: str = "0.0.0.0"):
    # Accepts ":8445", "host:8445", "0.0.0.0:8445".
    host, _, port = addr.rpartition(":")
    if host == "":
        host = default_host
    return host, int(port)


class DirectTransport(BaseTransport):
    def __init__(self, base: TransportConfig, cfg: DirectConfig) -> None:
        super().__init__(base)
        self._cfg = cfg
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        await super().start()
        self._closing = False
        if self._cfg.is_exit:
            host, port = _split_hostport(self._cfg.listen_addr or "0.0.0.0:0")
            self._server = await asyncio.start_server(self._serve_conn, host, port)
            sockname = self._server.sockets[0].getsockname()
            log.debugf("[DIRECT] exit listening on %s", sockname)
        else:
            if not self._cfg.dial_addr:
                raise ValueError("direct: dial_addr is empty")
            self._tasks.append(asyncio.ensure_future(self._dial_loop()))

    async def stop(self) -> None:
        self._closing = True
        if self._server:
            self._server.close()
        if self._writer:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        for t in self._tasks:
            t.cancel()
        self.set_connected(False)
        await super().stop()

    async def send(self, data: bytes) -> None:
        if not self.is_running():
            raise RuntimeError("direct: not running")
        if len(data) == 0 or len(data) > self._cfg.max_record_bytes:
            raise ValueError(f"direct: record size {len(data)} outside 1..{self._cfg.max_record_bytes}")
        writer = self._writer
        if writer is None:
            raise RuntimeError("direct: not connected")
        hdr = len(data).to_bytes(2, "big")
        async with self._write_lock:
            writer.write(hdr + data)
            await writer.drain()
        self.record_send(len(data))

    # ---- exit mode ----

    async def _serve_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.debugf("[DIRECT] accepted %s", peer)
        # Replace any previous peer.
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._reader = reader
        self._writer = writer
        self.set_connected(True)
        try:
            await self._read_loop(reader)
        finally:
            self.set_connected(False)
            if self._writer is writer:
                self._writer = None
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- client mode ----

    async def _dial_loop(self) -> None:
        host, port = _split_hostport(self._cfg.dial_addr)
        delay = self._cfg.reconnect_min_delay
        while not self._closing:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self._cfg.handshake_timeout,
                )
            except Exception as e:  # noqa: BLE001
                log.debugf("[DIRECT] dial %s: %s", self._cfg.dial_addr, e)
            else:
                log.debugf("[DIRECT] connected to %s", self._cfg.dial_addr)
                self._reader = reader
                self._writer = writer
                self.set_connected(True)
                self.record_reconnect()
                try:
                    await self._read_loop(reader)
                except Exception as e:  # noqa: BLE001
                    log.debugf("[DIRECT] serve: %s", e)
                self.set_connected(False)
                self._writer = None
            if self._closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * self._cfg.reconnect_multiplier, self._cfg.reconnect_max_delay)

    # ---- shared read loop ----

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._closing:
            try:
                hdr = await reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            n = int.from_bytes(hdr, "big")
            if n == 0 or n > self._cfg.max_record_bytes:
                log.debugf("[DIRECT] invalid record length %d", n)
                return
            try:
                body = await reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            self.record_receive(n)
            self.call_receive(body)
